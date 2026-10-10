"""Download the nightly database snapshot published as a GitHub release asset."""

import os
import sqlite3
from pathlib import Path

import httpx
import zstandard

from .contentdb import USER_AGENT

SNAPSHOT_URL = os.environ.get(
    "LDG_SNAPSHOT_URL",
    "https://github.com/culmat/luanti-depgraph/releases/download/snapshot/luanti-deps.sqlite.zst",
)


CODE_URL = os.environ.get(
    "LDG_CODE_URL",
    "https://github.com/culmat/luanti-depgraph/releases/download/snapshot/luanti-code.sqlite.zst",
)


def download_plain(target: Path, url: str) -> str:
    """Download and decompress a zstd file (no schema checks)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".download")
    with httpx.stream("GET", url, headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=300) as r:
        r.raise_for_status()
        decompressor = zstandard.ZstdDecompressor().decompressobj()
        with tmp.open("wb") as out:
            for chunk in r.iter_bytes():
                out.write(decompressor.decompress(chunk))
    tmp.replace(target)
    return f"code index → {target}"


def download(db_file: Path, url: str = SNAPSHOT_URL) -> dict:
    db_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = db_file.with_name(db_file.name + ".download")
    with httpx.stream("GET", url, headers={"User-Agent": USER_AGENT}, follow_redirects=True,
                      timeout=120) as response:
        response.raise_for_status()
        decompressor = zstandard.ZstdDecompressor().decompressobj()
        with tmp.open("wb") as out:
            for chunk in response.iter_bytes():
                out.write(decompressor.decompress(chunk))
    conn = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
    try:
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("downloaded snapshot failed the SQLite integrity check")
        built_at = conn.execute("SELECT value FROM meta WHERE key = 'built_at'").fetchone()[0]
    finally:
        conn.close()
    tmp.replace(db_file)
    return {"path": str(db_file), "built_at": built_at.strip('"'), "url": url}


def compress(db_file: Path, out: Path) -> Path:
    cctx = zstandard.ZstdCompressor(level=19)
    with db_file.open("rb") as src, out.open("wb") as dst:
        cctx.copy_stream(src, dst)
    return out
