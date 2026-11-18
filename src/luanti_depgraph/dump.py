"""ContentDB's daily database dump (https://content.luanti.org/help/backups/).

One zip, built at 05:00 UTC and served from ContentDB's CDN, holds every approved package
with its dependencies, details and full release list. A build reads it instead of making
thousands of API requests. The format is marked experimental upstream, so `load` checks it
and the build falls back to the API when it doesn't match.
"""

import json
import zipfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import NamedTuple

import httpx

from .contentdb import BASE_URL, USER_AGENT

FORMAT_VERSION = 1
PACKAGE_KEYS = ("author", "name", "type", "release", "provides", "depends", "optional_depends",
                "aliases")


class DumpUnavailable(Exception):
    """No dump could be downloaded."""


class DumpFormatError(Exception):
    """The dump doesn't look like the format this module was written for."""


class Dump(NamedTuple):
    raw: dict                                   # the same shape as the API lists (db.RAW_FILES)
    details: dict[tuple[str, str], dict]        # package JSON + latest_release + releases
    created_at: str


def file_name(day: date) -> str:
    return f"backup-{day.isoformat()}.zip"


def download(dump_dir: Path, log=print, today: date | None = None,
             client: httpx.Client | None = None) -> Path:
    """The newest dump: today's (UTC) if it is out yet, else yesterday's. Kept in `dump_dir`,
    so a dump is downloaded once; older ones are deleted."""
    today = today or datetime.now(UTC).date()
    dump_dir.mkdir(parents=True, exist_ok=True)
    own = client is None
    client = client or httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=300,
                                    follow_redirects=True)
    errors = []
    try:
        for day in (today, today - timedelta(days=1)):
            path = dump_dir / file_name(day)
            if not path.exists():
                try:
                    _fetch(client, f"{BASE_URL}/uploads/{path.name}", path)
                except httpx.HTTPError as exc:
                    errors.append(f"{path.name}: {exc}")
                    continue
                log(f"downloaded {path.name} ({path.stat().st_size // 1024 // 1024} MB)")
            for old in dump_dir.glob("backup-*.zip"):
                if old.name < path.name:
                    old.unlink()
            return path
    finally:
        if own:
            client.close()
    raise DumpUnavailable("; ".join(errors))


def _fetch(client: httpx.Client, url: str, path: Path) -> None:
    tmp = path.with_name(path.name + ".part")
    with client.stream("GET", url) as response:
        response.raise_for_status()
        with tmp.open("wb") as out:
            for chunk in response.iter_bytes():
                out.write(chunk)
    tmp.replace(path)


def load(path: Path) -> Dump:
    """Read a dump into the API-shaped lists the build uses, plus per-package details."""
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise DumpFormatError(f"{path.name}: {exc}") from exc
    with archive:
        names = set(archive.namelist())
        tops = sorted((n for n in names if n.endswith("index.json")), key=lambda n: n.count("/"))
        if not tops:
            raise DumpFormatError(f"{path.name}: no index.json")
        prefix = tops[0].removesuffix("index.json")
        index = json.loads(archive.read(tops[0]))
        if index.get("version") != FORMAT_VERSION:
            raise DumpFormatError(f"{path.name}: format version {index.get('version')!r},"
                                  f" expected {FORMAT_VERSION}")
        packages, details = [], {}
        depth = prefix.count("/") + 2  # <prefix><user>/<package>/index.json
        for name in sorted(names):
            if not (name.startswith(prefix) and name.endswith("/index.json")
                    and name.count("/") == depth):
                continue
            pkg = json.loads(archive.read(name))
            missing = [k for k in PACKAGE_KEYS if k not in pkg]
            if missing:
                raise DumpFormatError(f"{name}: missing {', '.join(missing)}")
            releases_name = name.removesuffix("index.json") + "releases.json"
            releases = json.loads(archive.read(releases_name)) if releases_name in names else []
            pkg["releases"] = releases
            pkg["latest_release"] = releases[0] if releases else None
            packages.append(pkg)
            details[(pkg["author"], pkg["name"])] = pkg
    if not packages:
        raise DumpFormatError(f"{path.name}: no packages")
    packages.sort(key=lambda p: (p["author"], p["name"]))
    return Dump(_raw(packages), details, index.get("created_at", ""))


def _raw(packages: list[dict]) -> dict:
    updates = {f"{p['author']}/{p['name']}": p["release"] for p in packages}
    for p in packages:
        for alias in p["aliases"]:
            updates.setdefault(f"{alias['author']}/{alias['name']}", p["release"])
    return {
        "packages": [{k: p.get(k) for k in ("author", "name", "type", "title", "short_description",
                                            "repo", "release")}
                     | _release_fields(p["latest_release"] or {})
                     for p in packages],
        "dependencies": [{k: p[k] for k in ("author", "name", "type", "provides", "depends",
                                            "optional_depends")} for p in packages],
        "scores": [{"author": p["author"], "name": p["name"], "score": p.get("score"),
                    "downloads": p.get("downloads")} for p in packages],
        "updates": updates,
    }


def _release_fields(release: dict) -> dict:
    """What the corpus needs to fetch exactly this release (see corpus.fetch_package)."""
    return {"commit": release.get("commit"), "release_url": release.get("url"),
            "release_sha256": release.get("sha256")}
