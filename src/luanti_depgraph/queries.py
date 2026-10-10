"""Query functions shared by the CLI and the MCP server. All return plain dicts."""

import re
import sqlite3
from collections import deque
from pathlib import Path

from .luanticonf import scan_package
from .resolve import Index, Pkg, Resolver, Target, compat, package_refs


def is_local(spec: str) -> bool:
    return spec in (".", "..") or spec.startswith(("/", "./", "../", "~")) or Path(spec).expanduser().is_dir()


def target_for(index: Index, spec: str) -> tuple[Target, Pkg | None]:
    if is_local(spec):
        return Target.from_scan(scan_package(Path(spec).expanduser())), None
    pkg = index.package(spec)
    return Target.from_pkg(pkg), pkg


def providers(index: Index, modname: str) -> dict:
    found = index.providers(modname)
    return {
        "modname": modname,
        "count": len(found),
        "mods": [p.summary() for p in found if p.type != "game"],
        "games": [p.summary() for p in found if p.type == "game"],
        "note": None if found else "No ContentDB package provides this mod name.",
    }


def _dep_summary(index: Index, name: str, optional: bool) -> dict:
    found = index.providers(name)
    mods = [p for p in found if p.type != "game"]
    games = [p for p in found if p.type == "game"]
    return {
        "modname": name,
        "optional": optional,
        "mod_providers": len(mods),
        "game_providers": len(games),
        "best_mod": mods[0].key if mods else None,
        "top_mods": [p.key for p in mods[:5]],
        "top_games": [p.name for p in games[:8]],
    }


def deps(index: Index, spec: str, tree: bool = False, game: str | None = None,
         max_depth: int = 6) -> dict:
    target, pkg = target_for(index, spec)
    out: dict = {
        "target": target.key,
        "type": target.type,
        "provides": sorted(target.provides),
        "depends": [_dep_summary(index, n, False) for n in sorted(target.depends)],
        "optional_depends": [_dep_summary(index, n, True) for n in sorted(target.optional)],
    }
    if pkg is not None:
        out["alternatives_with_same_name"] = [p.key for p in index.alternatives(pkg)]
    if game:
        out["resolution"] = Resolver(index, index.game(game)).resolve(target).as_dict()
    elif tree:
        def walk(names: set[str], seen: frozenset[str], depth: int) -> list[dict]:
            nodes = []
            for name in sorted(names):
                mods = [p for p in index.providers(name) if p.type != "game"]
                games = [p.name for p in index.providers(name) if p.type == "game"]
                node: dict = {"modname": name, "games": len(games)}
                if mods:
                    best = mods[0]
                    node["via"] = best.key
                    if best.key in seen:
                        node["cycle"] = True
                    elif depth < max_depth:
                        node["depends"] = walk(best.depends - best.provides, seen | {best.key}, depth + 1)
                else:
                    node["via"] = None
                    node["only_in_games"] = games[:8]
                nodes.append(node)
            return nodes
        out["tree"] = walk(target.depends, frozenset({target.key}), 1)
    return out


def rdeps(index: Index, spec: str, transitive: bool = False, hard_only: bool = False,
          max_depth: int = 6, limit: int = 500) -> dict:
    if "/" in spec:
        pkg = index.package(spec)
        names, origin = set(pkg.provides), pkg.key
    else:
        names, origin = {spec}, None
    found: dict[str, dict] = {}
    queue = deque((n, 1, None) for n in sorted(names))
    seen_names = set(names)
    while queue:
        name, depth, via = queue.popleft()
        for p in index.dependers_of.get(name, []):
            if p.key == origin or p.key in found:
                continue
            hard = name in p.depends
            if not hard and (hard_only or name not in p.optional):
                continue
            found[p.key] = {"package": p.key, "type": p.type, "via_modname": name,
                            "optional": not hard, "depth": depth, "via_package": via,
                            "score": round(p.score, 1)}
            if transitive and hard and depth < max_depth and p.type != "game":
                for provided in sorted(p.provides - seen_names):
                    seen_names.add(provided)
                    queue.append((provided, depth + 1, p.key))
    rows = sorted(found.values(), key=lambda r: (r["depth"], r["optional"], -r["score"]))
    return {"query": spec, "modnames": sorted(names), "count": len(rows), "dependents": rows[:limit],
            "truncated": len(rows) > limit}


def resolve(index: Index, spec: str, game: str) -> dict:
    target, _ = target_for(index, spec)
    return Resolver(index, index.game(game)).resolve(target).as_dict()


def compat_report(index: Index, spec: str, games: list[str] | None = None) -> dict:
    target, pkg = target_for(index, spec)
    refs = package_refs(index.conn, pkg) if pkg is not None else []
    if pkg is None:
        from .lint import lint  # local path: run the analyzer for references
        refs = lint(Path(spec).expanduser(), index.conn, index)["refs"]
    game_pkgs = [index.game(g) for g in games] if games else index.default_games()
    rows = compat(index, target, game_pkgs, refs)
    return {"target": target.key, "analyzed_sources": bool(refs) or pkg is None, "games": rows}


def name_check(index: Index, modname: str, author: str | None = None) -> dict:
    valid = bool(re.fullmatch(r"[a-z0-9_]+", modname))
    users = [p for p in index.providers_of.get(modname, []) if author is None or p.author != author]
    package_named = [p for p in index.by_name.get(modname, []) if author is None or p.author != author]
    taken = [p for p in users if p.type != "game"] + [p for p in package_named if p not in users]
    return {
        "modname": modname,
        "valid": valid,
        "available": valid and not taken,
        "used_by": [p.key for p in taken],
        "bundled_in_games": [p.key for p in users if p.type == "game"],
        "policy": "https://content.luanti.org/policy_and_guidance/ (3.1 right to a name,"
                  " 3.2 forks/drop-in replacements, 3.3 game prefixes)",
    }


def search(index: Index, query: str, type_: str | None = None, limit: int = 20) -> list[dict]:
    q = query.lower()
    hits = []
    for p in index.by_id.values():
        if type_ and p.type != type_:
            continue
        hay = f"{p.key} {p.title}".lower()
        if q in hay or q in p.provides:
            exact = q == p.name or q in p.provides
            hits.append((not exact, -p.score, p))
    hits.sort(key=lambda h: (h[0], h[1]))
    return [h[2].summary() | {"provides": sorted(h[2].provides)[:20]} for h in hits[:limit]]


_WRITE_SQL = re.compile(r"\b(insert|update|delete|drop|alter|create|replace|attach|detach|pragma|vacuum)\b", re.I)


def sql(conn: sqlite3.Connection, query: str, limit: int = 200) -> dict:
    if _WRITE_SQL.search(query):
        raise ValueError("Only read-only SELECT/WITH queries are allowed.")
    cur = conn.execute(query)
    columns = [d[0] for d in cur.description or []]
    rows = cur.fetchmany(limit + 1)
    return {"columns": columns, "rows": [list(r) for r in rows[:limit]], "truncated": len(rows) > limit}


def luacheck_globals(conn: sqlite3.Connection, path: Path) -> dict:
    """Globals defined by a mod's declared dependencies, for .luacheckrc read_globals."""
    pkg = scan_package(path)
    declared = {d for m in pkg.mods for d in m.depends + m.optional_depends} - pkg.provides
    found: dict[str, set[str]] = {}
    for global_name, modname in conn.execute(
            "SELECT DISTINCT g.global, m.name FROM mod_global g JOIN mod m ON m.id = g.mod_id"):
        if modname in declared:
            found.setdefault(global_name, set()).add(modname)
    for name in declared:  # convention: a mod's API table is named after the mod
        found.setdefault(name, set()).add(name)
    owned = {g for g, mods in found.items() if g in declared or len(mods) <= 2}
    return {"declared": sorted(declared), "read_globals": sorted(owned)}
