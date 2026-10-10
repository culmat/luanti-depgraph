"""Read Luanti content metadata from disk: game.conf, modpack.conf, mod.conf and
the legacy depends.txt / modpack.txt, following the engine's rules
(doc/lua_api.md "Mods", src/content/mods.cpp).
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

MODNAME_RE = re.compile(r"^[a-z0-9_]+$")


def parse_conf(text: str) -> dict[str, str]:
    """Parse the settings format: `key = value`, `#` comments, `\"\"\"` multi-line values."""
    out: dict[str, str] = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        i += 1
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if value.startswith('"""'):
            value = value[3:]
            parts = []
            while not value.endswith('"""') and i < len(lines):
                parts.append(value)
                value = lines[i]
                i += 1
            parts.append(value.removesuffix('"""'))
            value = "\n".join(parts).strip()
        out[key] = value
    return out


def split_names(value: str | None) -> list[str]:
    """The engine removes all whitespace and splits on commas."""
    if not value:
        return []
    return [n for n in re.sub(r"\s", "", value).split(",") if n]


def split_games(value: str | None) -> list[str]:
    """supported_games / unsupported_games; ContentDB ignores a `_game` suffix."""
    return [g.removesuffix("_game") if g != "*" else g for g in split_names(value)]


def read_conf(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    return parse_conf(path.read_text(encoding="utf-8", errors="replace"))


@dataclass
class ScannedMod:
    name: str
    path: str  # relative to the package root, posix, "" if the root is the mod
    dir: Path
    title: str = ""
    description: str = ""
    depends: list[str] = field(default_factory=list)
    optional_depends: list[str] = field(default_factory=list)
    has_mod_conf: bool = False
    legacy_depends_txt: bool = False


@dataclass
class ScannedPackage:
    type: str  # mod | modpack | game | txp | unknown
    name: str
    root: Path
    title: str = ""
    description: str = ""
    mods: list[ScannedMod] = field(default_factory=list)
    supported_games: list[str] = field(default_factory=list)
    unsupported_games: list[str] = field(default_factory=list)
    conf: dict[str, str] = field(default_factory=dict)

    @property
    def provides(self) -> set[str]:
        return {m.name for m in self.mods}

    @property
    def hard_depends(self) -> set[str]:
        return {d for m in self.mods for d in m.depends} - self.provides

    @property
    def optional_depends(self) -> set[str]:
        return {d for m in self.mods for d in m.optional_depends} - self.provides - self.hard_depends


def _is_modpack(path: Path) -> bool:
    return (path / "modpack.conf").is_file() or (path / "modpack.txt").is_file()


def _is_mod(path: Path) -> bool:
    return (path / "init.lua").is_file() or (path / "mod.conf").is_file()


def _subdirs(path: Path) -> list[Path]:
    return sorted(p for p in path.iterdir() if p.is_dir() and not p.name.startswith("."))


def read_mod(mod_dir: Path, root: Path) -> ScannedMod:
    conf = read_conf(mod_dir / "mod.conf")
    rel = mod_dir.relative_to(root).as_posix()
    mod = ScannedMod(
        name=conf.get("name") or mod_dir.name,
        path="" if rel == "." else rel,
        dir=mod_dir,
        title=conf.get("title", ""),
        description=conf.get("description", ""),
        has_mod_conf=(mod_dir / "mod.conf").is_file(),
    )
    if "depends" in conf or "optional_depends" in conf:
        mod.depends = split_names(conf.get("depends"))
        mod.optional_depends = split_names(conf.get("optional_depends"))
    elif (mod_dir / "depends.txt").is_file():
        mod.legacy_depends_txt = True
        for line in (mod_dir / "depends.txt").read_text(errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            if line.endswith("?"):
                mod.optional_depends.append(line[:-1])
            else:
                mod.depends.append(line)
    if not mod.description and (mod_dir / "description.txt").is_file():
        mod.description = (mod_dir / "description.txt").read_text(errors="replace").strip()
    return mod


def collect_mods(path: Path, root: Path) -> list[ScannedMod]:
    """Mods in a mods directory or modpack, recursing into nested modpacks."""
    mods: list[ScannedMod] = []
    for sub in _subdirs(path):
        if _is_modpack(sub):
            mods.extend(collect_mods(sub, root))
        elif _is_mod(sub):
            mods.append(read_mod(sub, root))
    return mods


def find_content_root(path: Path, depth: int = 2) -> Path:
    """Repos sometimes wrap the content in a single sub-directory."""
    if (path / "game.conf").is_file() or _is_modpack(path) or _is_mod(path) \
            or (path / "texture_pack.conf").is_file() or depth == 0:
        return path
    subs = _subdirs(path)
    if len(subs) == 1:
        return find_content_root(subs[0], depth - 1)
    return path


def scan_package(path: Path) -> ScannedPackage:
    root = find_content_root(Path(path).resolve())
    if (root / "game.conf").is_file():
        conf = read_conf(root / "game.conf")
        pkg = ScannedPackage("game", root.name.removesuffix("_game"), root, conf=conf)
        if (root / "mods").is_dir():
            pkg.mods = collect_mods(root / "mods", root)
    elif _is_modpack(root):
        conf = read_conf(root / "modpack.conf")
        pkg = ScannedPackage("modpack", conf.get("name") or root.name, root, conf=conf)
        pkg.mods = collect_mods(root, root)
    elif _is_mod(root):
        mod = read_mod(root, root)
        conf = read_conf(root / "mod.conf")
        pkg = ScannedPackage("mod", mod.name, root, conf=conf, mods=[mod])
    elif (root / "texture_pack.conf").is_file():
        conf = read_conf(root / "texture_pack.conf")
        pkg = ScannedPackage("txp", conf.get("name") or root.name, root, conf=conf)
    else:
        # A repo with several mods but no modpack.conf: treat it like a modpack.
        pkg = ScannedPackage("unknown", root.name, root)
        pkg.mods = collect_mods(root, root)
        if pkg.mods:
            pkg.type = "modpack"
    pkg.title = pkg.conf.get("title", "")
    pkg.description = pkg.conf.get("description", "")
    pkg.supported_games = split_games(pkg.conf.get("supported_games"))
    pkg.unsupported_games = split_games(pkg.conf.get("unsupported_games"))
    return pkg
