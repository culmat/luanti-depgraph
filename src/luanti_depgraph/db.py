"""The SQLite database: schema, views and the build pipeline.

Two levels of data live side by side:
  * package level (table `provides`, `dep`) straight from ContentDB, complete for
    every approved package;
  * mod level (tables `mod`, `mod_dep`, `mod_global`, `ref`) from scanning the
    sources in the corpus, available for the packages that were fetched.
"""

import json
import sqlite3
import time
from collections import defaultdict
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path

from . import __version__, analyze, buildinfo, dump
from . import corpus as corpus_mod
from .contentdb import ContentDB
from .luanticonf import ScannedPackage, scan_package
from .resolve import RULES_VERSION

# Bumped on every change to SCHEMA that is not purely additive (a renamed or removed table or
# column, a changed meaning). Readers of the snapshot pin the versions they understand.
# tests/test_contract.py fails when SCHEMA changes without a bump.
SCHEMA_VERSION = 1
# How many of `Index.default_games()` get precomputed compat_matrix / resolution rows.
PRECOMPUTED_GAMES = 15

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE package (
    id INTEGER PRIMARY KEY,
    author TEXT NOT NULL,
    name TEXT NOT NULL,
    type TEXT NOT NULL,                -- mod | game | txp
    title TEXT,
    short_description TEXT,
    repo TEXT,
    release_id INTEGER,
    downloads INTEGER,
    score REAL,
    license TEXT,
    media_license TEXT,
    dev_state TEXT,
    supports_all_games INTEGER,
    min_engine TEXT,
    max_engine TEXT,
    corpus_status TEXT,                -- NULL (not fetched) | pinned | cloned | zip | unchanged | failed
    corpus_ref TEXT,
    tags TEXT,                         -- JSON array of ContentDB tag names (with --details)
    created_at TEXT,                   -- ISO dates from ContentDB (with --details)
    approved_at TEXT,
    latest_release_at TEXT,
    release_count INTEGER,             -- approved releases (from the dump; NULL from the API)
    long_description TEXT,
    issue_tracker TEXT,                -- URLs as set on ContentDB
    forum_url TEXT,
    website TEXT,
    UNIQUE (author, name)
);

-- ContentDB users who may edit a package (the author is one of them).
CREATE TABLE package_maintainer (
    package_id INTEGER NOT NULL REFERENCES package (id),
    username TEXT NOT NULL,
    PRIMARY KEY (package_id, username)
);

CREATE TABLE package_alias (
    alias TEXT PRIMARY KEY,            -- old "author/name"
    package_id INTEGER NOT NULL REFERENCES package (id)
);

-- Technical mod names a package provides (every mod in a modpack or game).
CREATE TABLE provides (
    package_id INTEGER NOT NULL REFERENCES package (id),
    modname TEXT NOT NULL,
    PRIMARY KEY (package_id, modname)
);

-- Package-level dependencies as ContentDB reports them (self-provided names removed).
CREATE TABLE dep (
    package_id INTEGER NOT NULL REFERENCES package (id),
    modname TEXT NOT NULL,
    optional INTEGER NOT NULL,
    PRIMARY KEY (package_id, modname)
);

-- Individual mods found by scanning the package sources.
CREATE TABLE mod (
    id INTEGER PRIMARY KEY,
    package_id INTEGER NOT NULL REFERENCES package (id),
    name TEXT NOT NULL,
    path TEXT NOT NULL,                -- inside the package; '' when the package is the mod
    title TEXT,
    description TEXT,
    has_mod_conf INTEGER NOT NULL,
    legacy_depends_txt INTEGER NOT NULL,
    lua_files INTEGER NOT NULL,
    uses_minetest_namespace INTEGER NOT NULL,
    UNIQUE (package_id, path)
);

CREATE TABLE mod_dep (
    mod_id INTEGER NOT NULL REFERENCES mod (id),
    modname TEXT NOT NULL,
    optional INTEGER NOT NULL,
    PRIMARY KEY (mod_id, modname)
);

-- API globals a mod defines (a table assigned outside any function), used to attribute
-- global accesses to mods.
CREATE TABLE mod_global (
    mod_id INTEGER NOT NULL REFERENCES mod (id),
    global TEXT NOT NULL,
    PRIMARY KEY (mod_id, global)
);

-- References from a mod's code to other mod names, found by static analysis.
CREATE TABLE ref (
    mod_id INTEGER NOT NULL REFERENCES mod (id),
    modname TEXT NOT NULL,
    kind TEXT NOT NULL,                -- global | itemstring | modpath_guard
    symbol TEXT NOT NULL,
    count INTEGER NOT NULL,
    file TEXT,
    line INTEGER,
    declared INTEGER NOT NULL,         -- listed in depends/optional_depends
    transitive INTEGER NOT NULL,       -- not listed, but loaded earlier via a declared dependency
    guarded INTEGER NOT NULL,          -- the mod checks get_modpath()/global_exists() for it
    requires TEXT NOT NULL DEFAULT ''  -- comma-separated mods its file needs to be loaded at all
);

-- ContentDB's own game-support claims (only with `ldg build --details`).
CREATE TABLE game_support (
    package_id INTEGER NOT NULL REFERENCES package (id),
    game_id INTEGER NOT NULL REFERENCES package (id),
    supports INTEGER NOT NULL,
    confidence INTEGER NOT NULL,       -- 1 detected, 10 from .conf, 11 set on the website
    PRIMARY KEY (package_id, game_id)
);

-- Every approved release of a package (from the dump; only the latest from the API).
CREATE TABLE package_release (
    package_id INTEGER NOT NULL REFERENCES package (id),
    release_id INTEGER NOT NULL,       -- package.release_id is the latest
    title TEXT,
    released_at TEXT,                  -- ISO date
    commit_sha TEXT,                   -- git commit the release was made from, if any
    min_engine TEXT,
    max_engine TEXT,
    downloads INTEGER,
    size INTEGER,                      -- bytes of the release zip
    sha256 TEXT,
    PRIMARY KEY (package_id, release_id)
);

-- Precomputed answers of the resolver for every non-game package in the default games, so
-- consumers that read the snapshot (luanthetti, the explorer) don't have to re-implement the
-- rules, and so a port of the rules can be checked against every row. `meta.rules_version`
-- changes whenever the rules do.
CREATE TABLE compat_matrix (
    package_id INTEGER NOT NULL REFERENCES package (id),
    game_id INTEGER NOT NULL REFERENCES package (id),
    status TEXT NOT NULL,              -- ok | risky | unsatisfiable
    missing TEXT NOT NULL,             -- JSON array of mod names nobody can provide
    install TEXT NOT NULL,             -- JSON array of "author/name" to install
    undeclared_missing TEXT NOT NULL,  -- JSON array of "modname (kind)" undeclared references
    optional_available TEXT NOT NULL,  -- JSON array of optional dependencies that would be met
    PRIMARY KEY (package_id, game_id)
);
-- The full `resolve` answer (Resolution.as_dict()) where it says more than compat_matrix:
-- something to install, something missing, a shadowed game mod or a warning.
CREATE TABLE resolution (
    package_id INTEGER NOT NULL REFERENCES package (id),
    game_id INTEGER NOT NULL REFERENCES package (id),
    json TEXT NOT NULL,
    PRIMARY KEY (package_id, game_id)
);

-- How dependencies and provides changed across builds: one row per continuous interval in
-- which a package had the edge. Carried forward from the previous database on every build.
CREATE TABLE dep_history (
    package TEXT NOT NULL,             -- "author/name" (stable across builds)
    modname TEXT NOT NULL,
    optional INTEGER NOT NULL,
    first_seen TEXT NOT NULL,          -- build date (YYYY-MM-DD)
    last_seen TEXT NOT NULL,
    first_release INTEGER,
    last_release INTEGER,
    PRIMARY KEY (package, modname, optional, first_seen)
);
CREATE TABLE provides_history (
    package TEXT NOT NULL,
    modname TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    first_release INTEGER,
    last_release INTEGER,
    PRIMARY KEY (package, modname, first_seen)
);

CREATE INDEX provides_modname ON provides (modname);
CREATE INDEX dep_modname ON dep (modname);
CREATE INDEX package_maintainer_username ON package_maintainer (username);
CREATE INDEX mod_name ON mod (name);
CREATE INDEX mod_package ON mod (package_id);
CREATE INDEX mod_dep_modname ON mod_dep (modname);
CREATE INDEX ref_modname ON ref (modname);
CREATE INDEX ref_mod ON ref (mod_id);

CREATE VIEW v_package AS
    SELECT author || '/' || name AS package, * FROM package;

-- Releases whose minimum engine version differs from the release before.
CREATE VIEW v_release_engine_changes AS
    SELECT package, release_id, title, released_at, previous_min_engine, min_engine FROM (
        SELECT p.author || '/' || p.name AS package, r.release_id, r.title, r.released_at, r.min_engine,
               lag(r.min_engine) OVER w AS previous_min_engine, row_number() OVER w AS n
        FROM package_release r JOIN package p ON p.id = r.package_id
        WINDOW w AS (PARTITION BY r.package_id ORDER BY r.released_at, r.release_id))
    WHERE n > 1 AND min_engine IS NOT previous_min_engine;

CREATE VIEW v_providers AS
    SELECT pr.modname, p.author || '/' || p.name AS package, p.type, p.score,
           p.name = pr.modname AS exact_name
    FROM provides pr JOIN package p ON p.id = pr.package_id;

CREATE VIEW v_package_deps AS
    SELECT p.author || '/' || p.name AS package, p.type, d.modname, d.optional
    FROM dep d JOIN package p ON p.id = d.package_id;

CREATE VIEW v_game_modnames AS
    SELECT p.name AS game, p.author || '/' || p.name AS package, pr.modname
    FROM provides pr JOIN package p ON p.id = pr.package_id
    WHERE p.type = 'game';

CREATE VIEW v_dep_changes AS
    SELECT package, modname, optional, first_seen, last_seen,
           CASE WHEN last_seen = (SELECT max(last_seen) FROM dep_history) THEN 'current' ELSE 'removed' END AS state
    FROM dep_history;

CREATE VIEW v_undeclared_refs AS
    SELECT p.author || '/' || p.name AS package, m.name AS mod, r.modname, r.kind, r.symbol,
           r.count, r.file, r.line, r.guarded, r.requires,
           (EXISTS (SELECT 1 FROM provides x WHERE x.package_id = p.id AND x.modname = r.modname)
            OR EXISTS (SELECT 1 FROM mod y WHERE y.package_id = p.id AND y.name = r.modname))
               AS same_package
    FROM ref r JOIN mod m ON m.id = r.mod_id JOIN package p ON p.id = m.package_id
    WHERE r.declared = 0 AND r.transitive = 0;

-- Hard dependencies, transitively, through non-game providers. Filter on `root` early.
CREATE VIEW v_transitive_deps AS
    WITH RECURSIVE walk (root, modname, depth) AS (
        SELECT p.author || '/' || p.name, d.modname, 1
        FROM dep d JOIN package p ON p.id = d.package_id
        WHERE d.optional = 0
        UNION
        SELECT w.root, d2.modname, w.depth + 1
        FROM walk w
        JOIN provides pr ON pr.modname = w.modname
        JOIN package pp ON pp.id = pr.package_id AND pp.type != 'game'
        JOIN dep d2 ON d2.package_id = pr.package_id AND d2.optional = 0
        WHERE w.depth < 8
    )
    SELECT root, modname, min(depth) AS depth FROM walk GROUP BY root, modname;
"""

RAW_FILES = ("packages", "dependencies", "scores", "updates")


def connect(db_file: Path, readonly: bool = True) -> sqlite3.Connection:
    if not db_file.exists():
        raise FileNotFoundError(
            f"No database at {db_file}. Run `ldg update` (download the nightly snapshot) "
            "or `ldg build` (build from ContentDB).")
    uri = f"file:{db_file}?mode=ro" if readonly else f"file:{db_file}"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def _timed(seconds: dict[str, float], phase: str):
    """Record the duration of a build phase as `seconds_<phase>` in the build metadata."""
    started = time.monotonic()
    try:
        yield
    finally:
        seconds[f"seconds_{phase}"] = round(time.monotonic() - started, 1)


SOURCES = ("auto", "dump", "api")


def _fetch_raw(cdb: ContentDB, cache_dir: Path, source: str, offline: bool, log: Callable,
               seconds: dict[str, float]) -> tuple[dict, dump.Dump | None, dict]:
    """The package lists (RAW_FILES), the dump when it was the source (for details), and
    where they came from. `auto` prefers the daily dump and falls back to the API."""
    if source not in SOURCES:
        raise ValueError(f"source must be one of {', '.join(SOURCES)}")
    raw_dir = cache_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    origin_file = raw_dir / "source.json"
    if offline:
        raw = {name: json.loads((raw_dir / f"{name}.json").read_text()) for name in RAW_FILES}
        origin = json.loads(origin_file.read_text()) if origin_file.exists() else {"source": "api"}
        loaded = None
        if origin["source"] == "dump":
            zip_path = cache_dir / "dump" / origin["file"]
            if zip_path.exists():
                loaded = dump.load(zip_path)
            else:
                log(f"warning: {zip_path} is gone, building without package details")
        return raw, loaded, origin

    loaded = None
    if source in ("auto", "dump"):
        try:
            with _timed(seconds, "fetch_dump"):
                zip_path = dump.download(cache_dir / "dump", log)
                loaded = dump.load(zip_path)
        except (dump.DumpUnavailable, dump.DumpFormatError, OSError) as exc:
            if source == "dump":
                raise
            log(f"ContentDB dump not usable ({exc}); falling back to the API")
    if loaded is not None:
        raw = loaded.raw
        origin = {"source": "dump", "file": zip_path.name, "created_at": loaded.created_at}
        log(f"read {len(raw['packages'])} packages from {zip_path.name}")
    else:
        raw = {}
        for name in RAW_FILES:
            log(f"fetching {name} from ContentDB")
            with _timed(seconds, f"fetch_{name}"):
                raw[name] = getattr(cdb, name)()
        origin = {"source": "api"}
    for name in RAW_FILES:
        (raw_dir / f"{name}.json").write_text(json.dumps(raw[name]))
    origin_file.write_text(json.dumps(origin))
    keys = {(d["author"], d["name"]) for d in raw["dependencies"]}
    if len(keys) != len(raw["dependencies"]) or len(keys) != len(raw["packages"]):
        log(f"warning: dependencies for {len(keys)} packages ({len(raw['dependencies'])} items),"
            f" but {len(raw['packages'])} packages")
    return raw, loaded, origin


def _insert_packages(conn: sqlite3.Connection, raw: dict) -> dict[tuple[str, str], int]:
    scores = {(s["author"], s["name"]): s for s in raw["scores"]}
    ids: dict[tuple[str, str], int] = {}
    for p in raw["packages"]:
        score = scores.get((p["author"], p["name"]), {})
        cur = conn.execute(
            "INSERT INTO package (author, name, type, title, short_description, repo, release_id,"
            " downloads, score) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (p["author"], p["name"], p["type"], p.get("title"), p.get("short_description"),
             p.get("repo"), p.get("release"), score.get("downloads"), score.get("score")))
        ids[(p["author"], p["name"])] = cur.lastrowid
    # /api/updates/ lists old aliases with the same release id as the package they point to.
    by_release = {p.get("release"): ids[(p["author"], p["name"])]
                  for p in raw["packages"] if p.get("release")}
    for key, release in raw["updates"].items():
        author, _, name = key.partition("/")
        if (author, name) not in ids and release in by_release:
            conn.execute("INSERT OR IGNORE INTO package_alias VALUES (?, ?)", (key, by_release[release]))
    for d in raw["dependencies"]:
        pid = ids.get((d["author"], d["name"]))
        if pid is None:
            continue
        conn.executemany("INSERT OR IGNORE INTO provides VALUES (?, ?)",
                         [(pid, m) for m in d.get("provides", [])])
        conn.executemany("INSERT OR IGNORE INTO dep VALUES (?, ?, 0)",
                         [(pid, m) for m in d.get("depends", [])])
        conn.executemany("INSERT OR IGNORE INTO dep VALUES (?, ?, 1)",
                         [(pid, m) for m in d.get("optional_depends", [])])
    return ids


def _insert_details(conn: sqlite3.Connection, detail: Callable[[str, str, int | None], dict | None],
                    raw: dict, ids: dict[tuple[str, str], int], log: Callable) -> None:
    """Per-package details from `detail(author, name, release)`: the dump, or the API (cached)."""
    by_name_game = {p["name"]: ids[(p["author"], p["name"])]
                    for p in raw["packages"] if p["type"] == "game"}
    total = len(raw["packages"])
    for n, p in enumerate(raw["packages"], 1):
        if n % 250 == 0:
            log(f"package details {n}/{total}")
        try:
            d = detail(p["author"], p["name"], p.get("release"))
            if d is None:
                continue
        except Exception as exc:  # one bad package must not stop the build
            log(f"  details failed for {p['author']}/{p['name']}: {exc}")
            continue
        pid = ids[(p["author"], p["name"])]
        rel = d.get("latest_release") or {}
        releases = d["releases"] if "releases" in d else [rel] if rel else []
        conn.execute(
            "UPDATE package SET license = ?, media_license = ?, dev_state = ?, supports_all_games = ?,"
            " min_engine = ?, max_engine = ?, tags = ?, created_at = ?, approved_at = ?,"
            " latest_release_at = ?, release_count = ?, long_description = ?, issue_tracker = ?,"
            " forum_url = ?, website = ? WHERE id = ?",
            (d.get("license"), d.get("media_license"), d.get("dev_state"),
             int(bool(d.get("supports_all_games"))),
             (rel.get("min_minetest_version") or {}).get("name"),
             (rel.get("max_minetest_version") or {}).get("name"),
             json.dumps(sorted(d.get("tags") or [])), d.get("created_at"), d.get("approved_at"),
             rel.get("release_date"), len(releases) if "releases" in d else None,
             d.get("long_description"), d.get("issue_tracker"), d.get("forum_url"), d.get("website"),
             pid))
        conn.executemany("INSERT OR IGNORE INTO package_maintainer VALUES (?, ?)",
                         [(pid, m) for m in d.get("maintainers") or []])
        conn.executemany(
            "INSERT OR REPLACE INTO package_release VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(pid, r["id"], r.get("title"), r.get("release_date"), r.get("commit"),
              (r.get("min_minetest_version") or {}).get("name"),
              (r.get("max_minetest_version") or {}).get("name"),
              r.get("downloads"), r.get("size"), r.get("sha256")) for r in releases if r.get("id")])
        for gs in d.get("game_support") or []:
            game = gs.get("game") or {}
            game_id = ids.get((game.get("author"), game.get("name"))) or by_name_game.get(game.get("name"))
            if game_id is not None:
                conn.execute("INSERT OR REPLACE INTO game_support VALUES (?, ?, ?, ?)",
                             (ids[(p["author"], p["name"])], game_id, int(gs["supports"]),
                              gs["confidence"]))


def select_corpus_packages(raw: dict, top: int, include: list[str], all_: bool) -> list[dict]:
    scores = {(s["author"], s["name"]): s.get("score") or 0 for s in raw["scores"]}
    candidates = [p for p in raw["packages"] if p["type"] in ("mod", "game")]
    if all_:
        return candidates
    candidates.sort(key=lambda p: scores.get((p["author"], p["name"]), 0), reverse=True)
    chosen = candidates[:top]
    wanted = set(include)
    chosen += [p for p in candidates[top:]
               if f"{p['author']}/{p['name']}" in wanted or p["name"] in wanted]
    return chosen


def name_dependencies(conn: sqlite3.Connection,
                      scanned: dict[str, list[set[str]]] | None = None) -> dict[str, set[str]]:
    """For each mod name, the names guaranteed to load before it: the intersection of what
    every known provider declares (hard deps of standalone packages, declared deps of
    scanned mods). Games that weren't scanned are unknown and don't constrain the result."""
    per_provider: dict[str, list[set[str]]] = defaultdict(list)
    package_deps: dict[int, set[str]] = defaultdict(set)
    for pid, dep in conn.execute("SELECT package_id, modname FROM dep WHERE optional = 0"):
        package_deps[pid].add(dep)
    for modname, pid in conn.execute(
            "SELECT pr.modname, pr.package_id FROM provides pr"
            " JOIN package p ON p.id = pr.package_id AND p.type != 'game'"):
        per_provider[modname].append(package_deps.get(pid, set()))
    rows = conn.execute("SELECT m.id, m.name, d.modname FROM mod m LEFT JOIN mod_dep d ON d.mod_id = m.id")
    by_mod: dict[int, tuple[str, set[str]]] = {}
    for mod_id, name, dep in rows:
        entry = by_mod.setdefault(mod_id, (name, set()))
        if dep:
            entry[1].add(dep)
    for name, deps in by_mod.values():
        per_provider[name].append(deps)
    for name, dep_sets in (scanned or {}).items():
        per_provider[name].extend(dep_sets)
    return {name: set.intersection(*sets) for name, sets in per_provider.items() if sets}


def _package_facts(pkg: ScannedPackage, key: str, cache_file: Path | None,
                   reuse: bool) -> tuple[list[analyze.ModFacts], bool]:
    """Facts per mod of one corpus package, from the cache when its key (analyzer version and
    release marker) is unchanged. Returns (facts, whether they came from the cache)."""
    if reuse and cache_file is not None and cache_file.exists():
        cached = json.loads(cache_file.read_text())
        if cached.get("key") == key and all(m.path in cached["mods"] for m in pkg.mods):
            return [analyze.facts_from_json(cached["mods"][m.path]) for m in pkg.mods], True
    facts = [analyze.analyze_mod(m, pkg.root) for m in pkg.mods]
    if cache_file is not None:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        mods = {m.path: analyze.facts_to_json(f) for m, f in zip(pkg.mods, facts, strict=True)}
        cache_file.write_text(json.dumps({"key": key, "mods": mods}, separators=(",", ":")))
    return facts, False


def _analyze_corpus(conn: sqlite3.Connection, corpus: Path, ids: dict[tuple[str, str], int],
                    log: Callable, seconds: dict[str, float], cache_dir: Path | None = None,
                    reuse: bool = True) -> tuple[int, int]:
    """Analyze the corpus and insert mods and references. Returns (refs, cached packages)."""
    provided = {r[0] for r in conn.execute("SELECT DISTINCT modname FROM provides")}
    scanned = []  # (package_id, mods, facts per mod)
    analyzer = f"{analyze.ANALYZER_VERSION} tree-sitter-lua {metadata.version('tree-sitter-lua')}"
    cached = 0
    # Per package and independent of the others (cacheable), unlike the references below.
    with _timed(seconds, "analyze"):
        for marker in sorted(corpus.glob(f"*/*/{corpus_mod.MARKER}")):
            pkg_dir = marker.parent
            author, name = pkg_dir.parent.name, pkg_dir.name
            pid = ids.get((author, name))
            if pid is None:
                continue
            release = marker.read_text().strip()
            conn.execute("UPDATE package SET corpus_status = COALESCE(corpus_status, 'cached'),"
                         " corpus_ref = ? WHERE id = ?", (" ".join(release.split()[1:]), pid))
            pkg = scan_package(pkg_dir)
            cache_file = cache_dir / "analysis" / author / f"{name}.json" if cache_dir else None
            facts, hit = _package_facts(pkg, f"{analyzer} | {release}", cache_file, reuse)
            cached += hit
            scanned.append((pid, pkg, facts))
    log(f"analyzed {len(scanned)} corpus packages ({cached} unchanged, from the cache)")
    with _timed(seconds, "refs"):
        return _insert_mods(conn, scanned, provided), cached


def _insert_mods(conn: sqlite3.Connection, scanned: list, provided: set[str]) -> int:
    owners: dict[str, set[str]] = defaultdict(set)
    for _, pkg, facts in scanned:
        for mod, f in zip(pkg.mods, facts, strict=True):
            for g in f.api_globals:
                owners[g].add(mod.name)
    global_owners = analyze.filter_global_owners(owners)
    known = provided | {m.name for _, pkg, _ in scanned for m in pkg.mods}
    scanned_deps: dict[str, list[set[str]]] = defaultdict(list)
    for _, pkg, _ in scanned:
        for mod in pkg.mods:
            scanned_deps[mod.name].append(set(mod.depends) | set(mod.optional_depends))
    name_deps = name_dependencies(conn, scanned_deps)

    refs = 0
    for pid, pkg, facts in scanned:
        own = pkg.provides
        siblings = {m.name: set(m.depends) | set(m.optional_depends) for m in pkg.mods}
        for mod, f in zip(pkg.mods, facts, strict=True):
            cur = conn.execute(
                "INSERT OR IGNORE INTO mod (package_id, name, path, title, description, has_mod_conf,"
                " legacy_depends_txt, lua_files, uses_minetest_namespace)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (pid, mod.name, mod.path, mod.title, mod.description, int(mod.has_mod_conf),
                 int(mod.legacy_depends_txt), f.files, int(bool(f.minetest_namespace))))
            mod_id = cur.lastrowid
            conn.executemany("INSERT OR IGNORE INTO mod_dep VALUES (?, ?, 0)",
                             [(mod_id, d) for d in mod.depends])
            conn.executemany("INSERT OR IGNORE INTO mod_dep VALUES (?, ?, 1)",
                             [(mod_id, d) for d in mod.optional_depends])
            conn.executemany("INSERT OR IGNORE INTO mod_global VALUES (?, ?)",
                             [(mod_id, g) for g in f.api_globals])
            closure = analyze.dependency_closure(set(mod.depends) | set(mod.optional_depends),
                                                 siblings, name_deps)
            for r in analyze.references(mod, f, global_owners, known, own, closure):
                first = r.sites[0]
                conn.execute("INSERT INTO ref VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                             (mod_id, r.modname, r.kind, r.symbol, len(r.sites), first.file,
                              first.line, int(r.declared), int(r.transitive), int(r.guarded),
                              ",".join(r.requires)))
                refs += 1
    return refs


def _precompute(conn: sqlite3.Connection, log: Callable, games: int = PRECOMPUTED_GAMES) -> dict:
    """Fill compat_matrix and resolution for every non-game package in the default games."""
    from .resolve import Index, Resolver, Target, is_risky, package_refs
    conn.row_factory = sqlite3.Row
    index = Index(conn)
    game_pkgs = index.default_games(games)
    resolvers = [(g, Resolver(index, g)) for g in game_pkgs]
    packages = [p for p in index.by_id.values() if p.type != "game"]
    rows = resolutions = 0
    for pkg in packages:
        target = Target.from_pkg(pkg)
        refs = package_refs(conn, pkg)
        for game, resolver in resolvers:
            res = resolver.resolve(target)
            available = game.provides | target.provides | {
                m for entry in res.install for m in index.package(entry["package"]).provides}
            risky = sorted({(r["modname"], r["kind"]) for r in refs if is_risky(r, available)})
            status = "unsatisfiable" if not res.ok else ("risky" if risky else "ok")
            conn.execute("INSERT INTO compat_matrix VALUES (?, ?, ?, ?, ?, ?, ?)", (
                pkg.id, game.id, status,
                json.dumps([m["modname"] for m in res.missing]),
                json.dumps([e["package"] for e in res.install]),
                json.dumps([f"{m} ({k})" for m, k in risky]),
                json.dumps(res.optional_available)))
            rows += 1
            if res.install or res.missing or res.shadowed or res.warnings:
                conn.execute("INSERT INTO resolution VALUES (?, ?, ?)",
                             (pkg.id, game.id, json.dumps(res.as_dict(), separators=(",", ":"))))
                resolutions += 1
    conn.row_factory = None
    log(f"precomputed {rows} compat rows and {resolutions} resolutions for {len(game_pkgs)} games")
    return {"precomputed_games": [g.key for g in game_pkgs], "compat_rows": rows, "resolutions": resolutions}


def _carry_history(conn: sqlite3.Connection, previous: Path | None, today: str) -> dict:
    """Extend intervals that were current in the previous build, open new ones, keep closed ones."""
    old_dep: list[tuple] = []
    old_prov: list[tuple] = []
    prev_date = None
    if previous is not None and previous.exists():
        old = sqlite3.connect(f"file:{previous}?mode=ro", uri=True)
        try:
            tables = {r[0] for r in old.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            row = old.execute("SELECT value FROM meta WHERE key = 'built_at'").fetchone() if "meta" in tables else None
            prev_date = json.loads(row[0])[:10] if row else None
            if "dep_history" in tables:
                old_dep = old.execute("SELECT * FROM dep_history").fetchall()
                old_prov = old.execute("SELECT * FROM provides_history").fetchall()
            elif prev_date:  # previous build predates history: seed it from its current edges
                old_dep = [(k, m, o, prev_date, prev_date, r, r) for k, m, o, r in old.execute(
                    "SELECT p.author || '/' || p.name, d.modname, d.optional, p.release_id"
                    " FROM dep d JOIN package p ON p.id = d.package_id")]
                old_prov = [(k, m, prev_date, prev_date, r, r) for k, m, r in old.execute(
                    "SELECT p.author || '/' || p.name, pr.modname, p.release_id"
                    " FROM provides pr JOIN package p ON p.id = pr.package_id")]
        finally:
            old.close()

    current_dep = {(k, m, o): r for k, m, o, r in conn.execute(
        "SELECT p.author || '/' || p.name, d.modname, d.optional, p.release_id"
        " FROM dep d JOIN package p ON p.id = d.package_id")}
    current_prov = {(k, m): r for k, m, r in conn.execute(
        "SELECT p.author || '/' || p.name, pr.modname, p.release_id"
        " FROM provides pr JOIN package p ON p.id = pr.package_id")}

    def merge(old_rows: list[tuple], current: dict, width: int) -> list[tuple]:
        rows, extended = [], set()
        for row in old_rows:
            key, first, last, first_rel = row[:width], row[width], row[width + 1], row[width + 2]
            if last == prev_date and key in current and key not in extended:
                rows.append((*key, first, today, first_rel, current[key]))
                extended.add(key)
            else:
                rows.append(row)
        rows += [(*key, today, today, rel, rel) for key, rel in current.items() if key not in extended]
        return rows

    deps = merge(old_dep, current_dep, 3)
    provs = merge(old_prov, current_prov, 2)
    conn.executemany("INSERT OR REPLACE INTO dep_history VALUES (?, ?, ?, ?, ?, ?, ?)", deps)
    conn.executemany("INSERT OR REPLACE INTO provides_history VALUES (?, ?, ?, ?, ?, ?)", provs)
    return {"history_since": min([r[3] for r in deps] or [today]), "history_previous_build": prev_date}


def build(db_file: Path, cache_dir: Path, corpus: Path, *, details: bool = False,
          offline: bool = False, git_top: int = 0, git_include: list[str] | None = None,
          git_all: bool = False, analyze_only: bool = False, workers: int = 6,
          history_from: Path | None = None, source: str = "auto", log: Callable = print) -> dict:
    started = time.monotonic()
    seconds: dict[str, float] = {}
    cdb = ContentDB(cache_dir=cache_dir)
    raw, loaded, origin = _fetch_raw(cdb, cache_dir, source, offline or analyze_only, log, seconds)

    fetch_results = []
    if (git_top or git_include or git_all) and not analyze_only:
        selection = select_corpus_packages(raw, git_top, git_include or [], git_all)
        if loaded is None:  # API lists carry no release commit; the details cache may
            for p in selection:
                if d := cdb.package_details(p["author"], p["name"], p.get("release"), cached_only=True):
                    p.setdefault("commit", (d.get("latest_release") or {}).get("commit"))
        log(f"fetching sources for {len(selection)} packages into {corpus}")
        done = 0

        def progress(result):
            nonlocal done
            done += 1
            if result.status == "failed" or done % 50 == 0:
                log(f"  [{done}/{len(selection)}] {result.key}: {result.status} {result.detail}")

        with _timed(seconds, "sources"):
            fetch_results = corpus_mod.fetch_many(corpus, selection, workers=workers, progress=progress)

    db_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = db_file.with_name(db_file.name + ".building")
    tmp.unlink(missing_ok=True)
    conn = sqlite3.connect(tmp)
    conn.executescript(SCHEMA)
    ids = _insert_packages(conn, raw)
    with _timed(seconds, "details"):
        if loaded is not None:  # the dump has every package's details
            _insert_details(conn, lambda a, n, _r: loaded.details.get((a, n)), raw, ids,
                            lambda _m: None)
            details_from = "dump"
        elif details:
            log("fetching package details (cached by release id)")
            _insert_details(conn, cdb.package_details, raw, ids,
                            lambda m: log(f"{m} ({cdb.requests} requests so far)"))
            details_from = True
        elif (cache_dir / "packages").is_dir():
            _insert_details(conn, lambda a, n, r: cdb.package_details(a, n, r, cached_only=True),
                            raw, ids, lambda _m: None)
            details_from = "cached"
        else:
            details_from = False
    for result in fetch_results:
        author, _, name = result.key.partition("/")
        conn.execute("UPDATE package SET corpus_status = ? WHERE author = ? AND name = ?",
                     (result.status, author, name))
    # --analyze-only is for re-running a changed analyzer, so it ignores (and refreshes) the cache.
    refs, analysis_cached = (
        _analyze_corpus(conn, corpus, ids, log, seconds, cache_dir, reuse=not analyze_only)
        if corpus.is_dir() else (0, 0))
    today = datetime.now(UTC).date().isoformat()
    with _timed(seconds, "history"):
        history = _carry_history(conn, history_from if history_from is not None else db_file, today)
    with _timed(seconds, "precompute"):
        precomputed = _precompute(conn, log)

    stats = {
        "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "schema_version": SCHEMA_VERSION,
        "rules_version": RULES_VERSION,
        "ldg_version": __version__,
        **{f"ldg_{k}": v for k, v in buildinfo.source_commit().items()},
        "packages": len(raw["packages"]),
        "mods": sum(p["type"] == "mod" for p in raw["packages"]),
        "games": sum(p["type"] == "game" for p in raw["packages"]),
        "txps": sum(p["type"] == "txp" for p in raw["packages"]),
        "details": details_from,
        "contentdb_source": origin["source"],
        **({"dump_created_at": origin["created_at"]} if origin["source"] == "dump" else {}),
        "corpus_packages": conn.execute("SELECT count(DISTINCT package_id) FROM mod").fetchone()[0],
        "scanned_mods": conn.execute("SELECT count(*) FROM mod").fetchone()[0],
        "refs": refs,
        "analysis_cached_packages": analysis_cached,
        "contentdb_requests": cdb.requests,
        **history,
        **precomputed,
        **seconds,
        "build_seconds": round(time.monotonic() - started, 1),
    }
    conn.executemany("INSERT INTO meta VALUES (?, ?)", [(k, json.dumps(v)) for k, v in stats.items()])
    conn.commit()
    conn.execute("VACUUM")
    conn.close()
    tmp.replace(db_file)
    return stats


def meta(conn: sqlite3.Connection) -> dict:
    return {r["key"]: json.loads(r["value"]) for r in conn.execute("SELECT key, value FROM meta")}
