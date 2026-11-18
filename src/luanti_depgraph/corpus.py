"""Fetch package sources into a local corpus: <corpus>/<author>/<name>/.

Only code and metadata are materialized (Lua, .conf, depends.txt, README, LICENSE):
git repos are fetched shallow with a blob filter and a sparse checkout, at the commit of
the ContentDB release when it is known (so the analysis sees the code ContentDB serves),
else at the default branch; packages without a repo fall back to the ContentDB release
zip, extracting the same files. A `.ldg-release` marker records which ContentDB release a
checkout corresponds to, so unchanged packages are skipped on the next run:
  "<release> git <sha> release"  checked out at the release commit
  "<release> git <sha> head"     at the default branch (no release commit, or not fetchable)
  "<release> zip"                from the release zip
Markers of older versions ("<release> git <sha>") are fetched again once when the release
commit is known.
"""

import hashlib
import io
import os
import shutil
import subprocess
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

import httpx

from .contentdb import BASE_URL, USER_AGENT

SPARSE_PATTERNS = [
    "*.lua", "*.conf", "depends.txt", "description.txt", "modpack.txt",
    "README*", "LICENSE*", "COPYING*", "license.txt", ".luacheckrc",
]
MARKER = ".ldg-release"
MAX_ZIP_BYTES = 80 * 1024 * 1024
# Git hosts are many; ContentDB is one server. Keep its download load low.
_CONTENTDB_DOWNLOADS = threading.Semaphore(2)


@dataclass
class FetchResult:
    key: str
    status: str  # pinned | cloned | zip | unchanged | failed
    detail: str = ""


def package_dir(corpus: Path, author: str, name: str) -> Path:
    return corpus / author / name


def _wanted(member: str) -> bool:
    base = member.rsplit("/", 1)[-1]
    return any(fnmatch(base, pattern) for pattern in SPARSE_PATTERNS)


def _git(*args: str, cwd: Path | None = None, timeout: int = 300) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True,
                          timeout=timeout, check=True)


def clone_sparse(repo: str, dest: Path, commit: str | None = None) -> tuple[str, bool]:
    """Shallow, blob-filtered, sparse checkout of `commit`, or of the default branch when
    `commit` is None or the host won't serve it (deleted, force-pushed away, or a server
    that refuses fetching by id). Returns (checked-out commit, whether it is `commit`)."""
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    pinned = False
    if commit:
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            _git("init", "--quiet", str(tmp))
            _git("remote", "add", "origin", repo, cwd=tmp)
            _git("config", "remote.origin.promisor", "true", cwd=tmp)
            _git("config", "remote.origin.partialclonefilter", "blob:none", cwd=tmp)
            _git("sparse-checkout", "set", "--no-cone", *SPARSE_PATTERNS, cwd=tmp)
            _git("fetch", "--quiet", "--depth", "1", "--filter=blob:none", "origin", commit, cwd=tmp)
            _git("checkout", "--quiet", "FETCH_HEAD", cwd=tmp)
            pinned = True
        except subprocess.SubprocessError:
            pass
    if not pinned:
        shutil.rmtree(tmp, ignore_errors=True)
        _git("clone", "--quiet", "--depth", "1", "--filter=blob:none", "--no-checkout", repo, str(tmp))
        _git("sparse-checkout", "set", "--no-cone", *SPARSE_PATTERNS, cwd=tmp)
        _git("checkout", "--quiet", cwd=tmp)
    head = _git("rev-parse", "HEAD", cwd=tmp).stdout.strip()
    shutil.rmtree(dest, ignore_errors=True)
    tmp.rename(dest)
    return head, pinned


def extract_release_zip(author: str, name: str, dest: Path, client: httpx.Client,
                        url: str | None = None, sha256: str | None = None) -> None:
    """Extract the wanted files of the latest release zip. `url` (the release's CDN file, from
    the dump) skips ContentDB's download redirect, which also counts as a player download;
    `sha256` is checked when given."""
    url = url or f"{BASE_URL}/packages/{author}/{name}/download/"
    with client.stream("GET", url, follow_redirects=True) as response:
        response.raise_for_status()
        size = int(response.headers.get("content-length") or 0)
        if size > MAX_ZIP_BYTES:
            raise ValueError(f"release zip too large ({size // 1024 // 1024} MiB)")
        data = response.read()
    if sha256 and hashlib.sha256(data).hexdigest() != sha256.lower():
        raise ValueError("release zip doesn't match its sha256")
    tmp = dest.with_name(dest.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for member in archive.namelist():
            if member.endswith("/") or not _wanted(member) or ".." in member.split("/"):
                continue
            target = tmp / member
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(member))
    tmp.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(dest, ignore_errors=True)
    tmp.rename(dest)


def fetch_package(corpus: Path, pkg: dict, client: httpx.Client, prefer_zip: bool = False) -> FetchResult:
    author, name, release = pkg["author"], pkg["name"], pkg.get("release")
    key = f"{author}/{name}"
    dest = package_dir(corpus, author, name)
    marker = dest / MARKER
    if marker.is_file() and _current(marker.read_text().split(), release, pkg.get("commit")):
        return FetchResult(key, "unchanged")
    repo = pkg.get("repo")
    errors = []
    if repo and not prefer_zip:
        try:
            head, pinned = clone_sparse(repo, dest, pkg.get("commit"))
            marker.write_text(f"{release} git {head} {'release' if pinned else 'head'}\n")
            return FetchResult(key, "pinned" if pinned else "cloned", head)
        except (subprocess.SubprocessError, OSError) as exc:
            stderr = getattr(exc, "stderr", "") or ""
            errors.append(f"git: {stderr.strip().splitlines()[-1] if stderr.strip() else exc}")
    try:
        with _CONTENTDB_DOWNLOADS:
            extract_release_zip(author, name, dest, client, pkg.get("release_url"),
                                pkg.get("release_sha256"))
        marker.write_text(f"{release} zip\n")
        return FetchResult(key, "zip")
    except (httpx.HTTPError, zipfile.BadZipFile, ValueError, OSError) as exc:
        errors.append(f"zip: {exc}")
    return FetchResult(key, "failed", "; ".join(errors))


def _current(marker: list[str], release, commit: str | None) -> bool:
    """Whether a checkout with this marker is still the one to analyze for `release`."""
    if not marker or marker[0] != str(release):
        return False
    # A git checkout from before markers said release/head: fetch again at the release commit.
    return not (commit and len(marker) == 3 and marker[1] == "git")


def fetch_many(corpus: Path, packages: list[dict], workers: int = 6, progress=None) -> list[FetchResult]:
    results: list[FetchResult] = []
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=120) as client, \
            ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch_package, corpus, pkg, client): pkg for pkg in packages}
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            if progress:
                progress(result)
    return results
