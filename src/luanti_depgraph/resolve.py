"""Answer "can X run in game G, and what would have to be installed?".

The rules mirror the engine and the main-menu installer
(builtin/mainmenu/content/contentdb.lua, src/content/mod_configuration.cpp):
  * every mod bundled with the game is loaded and satisfies its mod name;
  * a hard dependency the game doesn't provide is installed from ContentDB,
    preferring the package whose name equals the mod name, then the
    best-scored other provider; games never satisfy dependencies of other content;
  * optional dependencies only matter if something else makes them available;
  * a user mod with the same name as a game mod replaces it (with a log warning).
Like ContentDB's game-support detection (app/domain/game_support.py), it also ignores the
Minetest Game copies bundled by a few games known to ship non-standard ones, and respects
the supported_games / unsupported_games a provider declares.
Unlike the installer, a candidate is only chosen if its own hard dependencies can
be met in that game, so the answer is "possible" rather than "what the menu picks".
"""

import sqlite3
from dataclasses import asdict, dataclass, field

from .luanticonf import ScannedPackage

# From ContentDB app/domain/game_support.py: games whose bundled copies of
# Minetest Game mods are not real MTG API implementations.
MTG_MODS = {
    "beds", "boats", "bucket", "carts", "default", "dungeon_loot", "env_sounds", "fire", "flowers",
    "give_initial_stuff", "map", "player_api", "sethome", "spawn", "tnt", "walls", "wool",
    "binoculars", "bones", "butterflies", "creative", "doors", "dye", "farming", "fireflies",
    "game_commands", "keys", "mtg_craftguide", "screwdriver", "sfinv", "stairs", "vessels",
    "weather", "xpanes",
}
GAMES_WITH_NONSTANDARD_MTG_COPIES = {
    "pacman", "tutorial", "runorfall", "realtest_mt5", "mevo", "xaenvironment",
    "survivethedays", "holidayhorrors",
}
REFERENCE_GAMES = ("minetest_game", "mineclonia", "mineclone2")


@dataclass
class Pkg:
    id: int
    author: str
    name: str
    type: str
    title: str
    score: float
    downloads: int
    provides: set[str] = field(default_factory=set)
    depends: set[str] = field(default_factory=set)
    optional: set[str] = field(default_factory=set)

    @property
    def key(self) -> str:
        return f"{self.author}/{self.name}"

    def summary(self) -> dict:
        return {"package": self.key, "type": self.type, "title": self.title,
                "score": round(self.score or 0, 1), "downloads": self.downloads}


def _norm(value: str) -> str:
    return value.strip().lower().removesuffix("_game").replace(" ", "")


class Index:
    """All packages with their provides and dependencies, held in memory."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.by_id: dict[int, Pkg] = {}
        for r in conn.execute("SELECT id, author, name, type, title, score, downloads FROM package"):
            self.by_id[r[0]] = Pkg(r[0], r[1], r[2], r[3], r[4] or "", r[5] or 0.0, r[6] or 0)
        for pid, modname in conn.execute("SELECT package_id, modname FROM provides"):
            self.by_id[pid].provides.add(modname)
        for pid, modname, optional in conn.execute("SELECT package_id, modname, optional FROM dep"):
            (self.by_id[pid].optional if optional else self.by_id[pid].depends).add(modname)
        self.by_key = {p.key: p for p in self.by_id.values()}
        for alias, pid in conn.execute("SELECT alias, package_id FROM package_alias"):
            self.by_key.setdefault(alias, self.by_id[pid])
        self.by_name: dict[str, list[Pkg]] = {}
        self.providers_of: dict[str, list[Pkg]] = {}
        self.dependers_of: dict[str, list[Pkg]] = {}
        for p in self.by_id.values():
            self.by_name.setdefault(p.name, []).append(p)
            for m in p.provides:
                self.providers_of.setdefault(m, []).append(p)
            for m in p.depends | p.optional:
                self.dependers_of.setdefault(m, []).append(p)
        self.games = sorted((p for p in self.by_id.values() if p.type == "game"),
                            key=lambda p: -p.score)
        # Support declared by authors (mod.conf or website), as opposed to detected (confidence 1).
        self.declared_support: dict[int, set[int]] = {}
        self.declared_unsupported: dict[int, set[int]] = {}
        for pid, gid, supports in conn.execute(
                "SELECT package_id, game_id, supports FROM game_support WHERE confidence > 5"):
            (self.declared_support if supports else self.declared_unsupported).setdefault(pid, set()).add(gid)
        self.supports_all = {r[0] for r in conn.execute("SELECT id FROM package WHERE supports_all_games = 1")}
        self._independent: dict[int, bool] = {}

    def game_independent(self, pkg: "Pkg") -> bool:
        """True if the package's hard dependencies can be met by standalone mods alone."""
        if pkg.id not in self._independent:
            nowhere = Pkg(-1, "", "", "game", "", 0.0, 0)
            self._independent[pkg.id] = Resolver(self, nowhere, apply_declared=False)._satisfiable(
                pkg, frozenset())[0]
        return self._independent[pkg.id]

    def package(self, spec: str) -> Pkg:
        spec = spec.strip().strip("/")
        if spec in self.by_key:
            return self.by_key[spec]
        lowered = {k.lower(): p for k, p in self.by_key.items()}
        if spec.lower() in lowered:
            return lowered[spec.lower()]
        matches = self.by_name.get(spec) or self.by_name.get(spec.lower())
        if matches:
            return max(matches, key=lambda p: (p.type != "txp", p.score))
        raise LookupError(f"No ContentDB package '{spec}'. Use author/name, e.g. mt-mods/pipeworks.")

    def alternatives(self, pkg: Pkg) -> list[Pkg]:
        return [p for p in self.by_name.get(pkg.name, []) if p.id != pkg.id]

    def game(self, spec: str) -> Pkg:
        if spec in self.by_key and self.by_key[spec].type == "game":
            return self.by_key[spec]
        want = _norm(spec)
        for g in self.games:
            if want in (_norm(g.name), _norm(g.title), _norm(g.key)):
                return g
        if want == "minetest":  # engine game id of Minetest Game
            return self.game("minetest_game")
        raise LookupError(f"No game '{spec}' on ContentDB. Try one of: "
                          + ", ".join(g.name for g in self.games[:12]))

    def default_games(self, n: int = 10) -> list[Pkg]:
        chosen = [self.game(g) for g in REFERENCE_GAMES if any(_norm(x.name) == _norm(g) for x in self.games)]
        for g in self.games:
            if len(chosen) >= n:
                break
            if g not in chosen:
                chosen.append(g)
        return chosen

    def providers(self, modname: str) -> list[Pkg]:
        """Installer preference: exact-name non-game package, other non-game providers by
        score, then games (which only count when they are the game being played)."""
        return sorted(self.providers_of.get(modname, []),
                      key=lambda p: (p.type == "game", p.name != modname, -p.score))


@dataclass
class Target:
    key: str
    type: str
    provides: set[str]
    depends: set[str]
    optional: set[str]

    @classmethod
    def from_pkg(cls, pkg: Pkg) -> "Target":
        return cls(pkg.key, pkg.type, set(pkg.provides), pkg.depends - pkg.provides,
                   pkg.optional - pkg.provides)

    @classmethod
    def from_scan(cls, scanned: ScannedPackage) -> "Target":
        return cls(f"local:{scanned.name}", scanned.type, scanned.provides,
                   scanned.hard_depends, scanned.optional_depends)


@dataclass
class Resolution:
    target: str
    game: str
    ok: bool
    from_game: list[str] = field(default_factory=list)
    install: list[dict] = field(default_factory=list)
    missing: list[dict] = field(default_factory=list)
    shadowed: list[dict] = field(default_factory=list)
    optional_available: list[str] = field(default_factory=list)
    optional_unavailable: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


class Resolver:
    def __init__(self, index: Index, game: Pkg, apply_declared: bool = True):
        self.index = index
        self.game = game
        self.game_names = set(game.provides)
        if game.name in GAMES_WITH_NONSTANDARD_MTG_COPIES:
            self.game_names -= MTG_MODS
        self.apply_declared = apply_declared
        self._memo: dict[str, bool] = {}

    def _declared_ok(self, pkg: Pkg) -> bool:
        """A provider that declares its games only counts in those (as on ContentDB)."""
        if not self.apply_declared:
            return True
        if self.game.id in self.index.declared_unsupported.get(pkg.id, ()):
            return False
        declared = self.index.declared_support.get(pkg.id)
        if declared and pkg.id not in self.index.supports_all and self.game.id not in declared:
            return not self.index.game_independent(pkg)
        return True

    def _satisfiable(self, pkg: Pkg, stack: frozenset[str]) -> tuple[bool, bool]:
        """(ok, cycle_cut). A result influenced by a cut cycle isn't memoized."""
        if pkg.type == "game":
            return False, False
        if pkg.key in self._memo:
            return self._memo[pkg.key], False
        if pkg.key in stack:
            return False, True
        if not self._declared_ok(pkg):
            self._memo[pkg.key] = False
            return False, False
        stack = stack | {pkg.key}
        cut = False
        for name in sorted(pkg.depends - pkg.provides):
            ok, c = self._name_ok(name, stack)
            cut |= c
            if not ok:
                if not cut:
                    self._memo[pkg.key] = False
                return False, cut
        self._memo[pkg.key] = True
        return True, cut

    def _name_ok(self, name: str, stack: frozenset[str]) -> tuple[bool, bool]:
        if name in self.game_names:
            return True, False
        cut = False
        for p in self.index.providers(name):
            ok, c = self._satisfiable(p, stack)
            cut |= c
            if ok:
                return True, cut
        return False, cut

    def best_provider(self, name: str) -> Pkg | None:
        for p in self.index.providers(name):
            if self._satisfiable(p, frozenset())[0]:
                return p
        return None

    def explain_missing(self, name: str, needed_by: str) -> dict:
        found = self.index.providers(name)
        games = [p.key for p in found if p.type == "game"]
        candidates = []
        for p in found:
            if p.type == "game":
                continue
            failing = [d for d in sorted(p.depends - p.provides)
                       if not self._name_ok(d, frozenset({p.key}))[0]]
            problem = f"needs {', '.join(failing)}, not available in this game" if failing \
                else "dependency cycle"
            candidates.append({"package": p.key, "problem": problem})
            if len(candidates) == 10:
                break
        return {"modname": name, "needed_by": needed_by, "providers": len(found),
                "candidates": candidates, "only_in_games": games[:10], "game_providers": len(games)}

    def resolve(self, target: Target) -> Resolution:
        res = Resolution(target.key, self.game.key, ok=True)
        if target.type == "game":
            res.warnings.append("The target is a game; games are played, not installed into games.")
        if self.game.name in GAMES_WITH_NONSTANDARD_MTG_COPIES:
            res.warnings.append(f"{self.game.key} bundles copies of Minetest Game mods that are known "
                                "not to implement the MTG API; like ContentDB, they are ignored here.")
        for name in sorted(target.provides & self.game_names):
            res.shadowed.append({"modname": name, "by": target.key,
                                 "note": f"replaces the game's own '{name}' mod"})

        chosen: dict[str, str] = {}
        installed: dict[str, dict] = {}

        def need(name: str, needed_by: str) -> None:
            if name in target.provides or name in chosen:
                return
            if name in self.game_names:
                chosen[name] = "game"
                res.from_game.append(name)
                return
            provider = self.best_provider(name)
            if provider is None:
                res.missing.append(self.explain_missing(name, needed_by))
                chosen[name] = "missing"
                return
            entry = installed.get(provider.key)
            if entry is None:
                entry = {"package": provider.key, "for": [], "needed_by": needed_by,
                         "alternatives": len(self.index.providers(name)) - 1}
                installed[provider.key] = entry
                for provided in sorted(provider.provides):
                    if provided in self.game_names:
                        res.shadowed.append({"modname": provided, "by": provider.key,
                                             "note": f"replaces the game's own '{provided}' mod"})
                    chosen.setdefault(provided, provider.key)
                for dep in sorted(provider.depends - provider.provides):
                    need(dep, provider.key)
            entry["for"].append(name)

        for name in sorted(target.depends):
            need(name, target.key)
        res.install = list(installed.values())
        available = self.game_names | set(chosen) | target.provides
        res.optional_available = sorted(n for n in target.optional if n in available)
        res.optional_unavailable = sorted(n for n in target.optional if n not in available)
        res.ok = not res.missing
        return res


def package_refs(conn: sqlite3.Connection, pkg: Pkg) -> list[dict]:
    """Static-analysis references recorded for a package's mods (empty if not in the corpus)."""
    rows = conn.execute(
        "SELECT m.name AS mod, r.modname, r.kind, r.symbol, r.count, r.file, r.line, r.declared,"
        " r.transitive, r.guarded FROM ref r JOIN mod m ON m.id = r.mod_id WHERE m.package_id = ?", (pkg.id,))
    return [dict(r) for r in rows]


def compat(index: Index, target: Target, games: list[Pkg], refs: list[dict]) -> list[dict]:
    """One row per game: can the target be installed, and do its undeclared references hold?"""
    rows = []
    claims = {}
    if not target.key.startswith("local:"):
        pkg = index.package(target.key)
        claims = {r[0]: (bool(r[1]), r[2]) for r in index.conn.execute(
            "SELECT game_id, supports, confidence FROM game_support WHERE package_id = ?", (pkg.id,))}
    for game in games:
        res = Resolver(index, game).resolve(target)
        available = game.provides | target.provides | {
            m for entry in res.install for m in index.package(entry["package"]).provides}
        risky = sorted({(r["modname"], r["kind"]) for r in refs
                        if not (r["declared"] or r["transitive"] or r["guarded"]) and r["modname"] not in available})
        status = "unsatisfiable" if not res.ok else ("risky" if risky else "ok")
        claim = claims.get(game.id)
        rows.append({
            "game": game.key,
            "title": game.title,
            "status": status,
            "missing": [m["modname"] for m in res.missing],
            "install": [e["package"] for e in res.install],
            "undeclared_missing": [f"{m} ({k})" for m, k in risky],
            "optional_available": res.optional_available,
            "contentdb_says": None if claim is None else
            {"supports": claim[0], "confidence": claim[1]},
        })
    return rows
