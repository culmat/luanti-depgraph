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
from datetime import UTC, datetime
from pathlib import Path

from . import __version__, analyze
from . import corpus as corpus_mod
from .contentdb import ContentDB
from .luanticonf import scan_package

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
    corpus_status TEXT,                -- NULL (not fetched) | cloned | zip | unchanged | failed
    corpus_ref TEXT,
    UNIQUE (author, name)
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
    guarded INTEGER NOT NULL           -- the mod checks get_modpath()/global_exists() for it
);

-- ContentDB's own game-support claims (only with `ldg build --details`).
CREATE TABLE game_support (
    package_id INTEGER NOT NULL REFERENCES package (id),
    game_id INTEGER NOT NULL REFERENCES package (id),
    supports INTEGER NOT NULL,
    confidence INTEGER NOT NULL,       -- 1 detected, 10 from .conf, 11 set on the website
    PRIMARY KEY (package_id, game_id)
);

CREATE INDEX provides_modname ON provides (modname);
CREATE INDEX dep_modname ON dep (modname);
CREATE INDEX mod_name ON mod (name);
CREATE INDEX mod_package ON mod (package_id);
CREATE INDEX mod_dep_modname ON mod_dep (modname);
CREATE INDEX ref_modname ON ref (modname);
CREATE INDEX ref_mod ON ref (mod_id);

CREATE VIEW v_package AS
    SELECT author || '/' || name AS package, * FROM package;

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

CREATE VIEW v_undeclared_refs AS
    SELECT p.author || '/' || p.name AS package, m.name AS mod, r.modname, r.kind, r.symbol,
           r.count, r.file, r.line, r.guarded,
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


def _fetch_raw(cdb: ContentDB, raw_dir: Path, offline: bool, log: Callable) -> dict:
    raw: dict = {}
    raw_dir.mkdir(parents=True, exist_ok=True)
    for name in RAW_FILES:
        path = raw_dir / f"{name}.json"
        if offline:
            raw[name] = json.loads(path.read_text())
            continue
        log(f"fetching {name} from ContentDB")
        raw[name] = getattr(cdb, name)()
        path.write_text(json.dumps(raw[name]))
    return raw


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


def _insert_details(conn: sqlite3.Connection, cdb: ContentDB, raw: dict,
                    ids: dict[tuple[str, str], int], log: Callable) -> None:
    by_name_game = {p["name"]: ids[(p["author"], p["name"])]
                    for p in raw["packages"] if p["type"] == "game"}
    total = len(raw["packages"])
    for n, p in enumerate(raw["packages"], 1):
        if n % 250 == 0:
            log(f"package details {n}/{total} ({cdb.requests} requests so far)")
        try:
            d = cdb.package_details(p["author"], p["name"], p.get("release"))
        except Exception as exc:  # one bad package must not stop the build
            log(f"  details failed for {p['author']}/{p['name']}: {exc}")
            continue
        rel = d.get("latest_release") or {}
        conn.execute(
            "UPDATE package SET license = ?, media_license = ?, dev_state = ?, supports_all_games = ?,"
            " min_engine = ?, max_engine = ? WHERE id = ?",
            (d.get("license"), d.get("media_license"), d.get("dev_state"),
             int(bool(d.get("supports_all_games"))),
             (rel.get("min_minetest_version") or {}).get("name"),
             (rel.get("max_minetest_version") or {}).get("name"),
             ids[(p["author"], p["name"])]))
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


def _analyze_corpus(conn: sqlite3.Connection, corpus: Path, ids: dict[tuple[str, str], int],
                    log: Callable) -> int:
    provided = {r[0] for r in conn.execute("SELECT DISTINCT modname FROM provides")}
    scanned = []  # (package_id, mods, facts per mod)
    for marker in sorted(corpus.glob(f"*/*/{corpus_mod.MARKER}")):
        pkg_dir = marker.parent
        pid = ids.get((pkg_dir.parent.name, pkg_dir.name))
        if pid is None:
            continue
        status = marker.read_text().split()
        conn.execute("UPDATE package SET corpus_status = COALESCE(corpus_status, 'cached'),"
                     " corpus_ref = ? WHERE id = ?", (" ".join(status[1:]), pid))
        pkg = scan_package(pkg_dir)
        facts = [analyze.analyze_mod(m, pkg.root) for m in pkg.mods]
        scanned.append((pid, pkg, facts))
    log(f"analyzed {len(scanned)} corpus packages")

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
                conn.execute("INSERT INTO ref VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                             (mod_id, r.modname, r.kind, r.symbol, len(r.sites), first.file,
                              first.line, int(r.declared), int(r.transitive), int(r.guarded)))
                refs += 1
    return refs


def build(db_file: Path, cache_dir: Path, corpus: Path, *, details: bool = False,
          offline: bool = False, git_top: int = 0, git_include: list[str] | None = None,
          git_all: bool = False, analyze_only: bool = False, workers: int = 6,
          log: Callable = print) -> dict:
    started = time.monotonic()
    cdb = ContentDB(cache_dir=cache_dir)
    raw = _fetch_raw(cdb, cache_dir / "raw", offline or analyze_only, log)

    fetch_results = []
    if (git_top or git_include or git_all) and not analyze_only:
        selection = select_corpus_packages(raw, git_top, git_include or [], git_all)
        log(f"fetching sources for {len(selection)} packages into {corpus}")
        done = 0

        def progress(result):
            nonlocal done
            done += 1
            if result.status == "failed" or done % 50 == 0:
                log(f"  [{done}/{len(selection)}] {result.key}: {result.status} {result.detail}")

        fetch_results = corpus_mod.fetch_many(corpus, selection, workers=workers, progress=progress)

    db_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = db_file.with_name(db_file.name + ".building")
    tmp.unlink(missing_ok=True)
    conn = sqlite3.connect(tmp)
    conn.executescript(SCHEMA)
    ids = _insert_packages(conn, raw)
    if details:
        log("fetching package details (cached by release id)")
        _insert_details(conn, cdb, raw, ids, log)
    for result in fetch_results:
        author, _, name = result.key.partition("/")
        conn.execute("UPDATE package SET corpus_status = ? WHERE author = ? AND name = ?",
                     (result.status, author, name))
    refs = _analyze_corpus(conn, corpus, ids, log) if corpus.is_dir() else 0

    stats = {
        "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "ldg_version": __version__,
        "packages": len(raw["packages"]),
        "mods": sum(p["type"] == "mod" for p in raw["packages"]),
        "games": sum(p["type"] == "game" for p in raw["packages"]),
        "txps": sum(p["type"] == "txp" for p in raw["packages"]),
        "details": details,
        "corpus_packages": conn.execute("SELECT count(DISTINCT package_id) FROM mod").fetchone()[0],
        "scanned_mods": conn.execute("SELECT count(*) FROM mod").fetchone()[0],
        "refs": refs,
        "contentdb_requests": cdb.requests,
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
