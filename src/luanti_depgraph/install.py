"""Materialize what a target needs to run in a game: resolve, download release zips
(cached by release id) and work out exactly which mods to enable.

Only the mods that are needed get enabled, including inside modpacks: enabling
every mod of a modpack could pull in mods whose own dependencies aren't met,
which the engine treats as fatal.
"""

import io
import shutil
import zipfile
from pathlib import Path

import httpx

from .contentdb import BASE_URL, USER_AGENT
from .luanticonf import ScannedPackage, find_content_root, scan_package
from .queries import target_for
from .resolve import Index, Pkg, Resolver


def download_release(pkg: Pkg, release: int | None, cache: Path) -> Path:
    """Extract the package's latest release into the cache and return its content root."""
    dest = cache / "releases" / pkg.author / pkg.name / str(release or "latest")
    content = dest / "content"
    if not (dest / ".complete").is_file():
        shutil.rmtree(dest, ignore_errors=True)
        url = f"{BASE_URL}/packages/{pkg.author}/{pkg.name}/download/"
        with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=300, follow_redirects=True) as client:
            response = client.get(url)
            response.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            for member in archive.infolist():
                parts = Path(member.filename).parts
                if member.is_dir() or ".." in parts or Path(member.filename).is_absolute():
                    continue
                target = content / member.filename
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(member))
        (dest / ".complete").write_text(url + "\n")
    return find_content_root(content)


def _mods_to_enable(scanned: ScannedPackage, needed: set[str]) -> set[str]:
    """Needed mod names plus their hard dependencies inside the same package."""
    by_name = {m.name: m for m in scanned.mods}
    enable: set[str] = set()
    queue = [n for n in needed if n in by_name]
    while queue:
        name = queue.pop()
        if name in enable:
            continue
        enable.add(name)
        queue.extend(d for d in by_name[name].depends if d in by_name)
    return enable


def _copy(root: Path, dest: Path) -> None:
    shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(root, dest, ignore=shutil.ignore_patterns(".git", ".github", ".ldg-release"))


def install(index: Index, spec: str, game: str, dest: Path, cache: Path,
            extra: list[str] | None = None) -> dict:
    target, pkg = target_for(index, spec)
    target.depends |= set(extra or []) - target.provides
    resolution = Resolver(index, index.game(game)).resolve(target)
    result: dict = {"ok": resolution.ok, "game": resolution.game, "target": target.key,
                    "resolution": resolution.as_dict(), "installed": [], "enable": []}
    if not resolution.ok:
        return result
    dest.mkdir(parents=True, exist_ok=True)
    release_of = {r[0]: r[1] for r in index.conn.execute(
        "SELECT author || '/' || name, release_id FROM package")}

    enable: set[str] = set()
    if pkg is not None:
        root = download_release(pkg, release_of.get(pkg.key), cache)
        scanned = scan_package(root)
        folder = dest / (scanned.mods[0].name if scanned.type == "mod" else pkg.name)
        _copy(root, folder)
        enable |= scanned.provides
        result["installed"].append({"package": pkg.key, "path": str(folder), "role": "target"})
    else:
        enable |= target.provides

    for entry in resolution.install:
        dep = index.package(entry["package"])
        root = download_release(dep, release_of.get(dep.key), cache)
        scanned = scan_package(root)
        folder = dest / (scanned.mods[0].name if scanned.type == "mod" else dep.name)
        _copy(root, folder)
        mods = _mods_to_enable(scanned, set(entry["for"]))
        enable |= mods
        result["installed"].append({"package": dep.key, "path": str(folder), "for": entry["for"],
                                    "enable": sorted(mods)})
    result["enable"] = sorted(enable)
    return result
