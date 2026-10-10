"""Build the static dependency explorer: the web app plus precomputed data.

`ldg site --out DIR` writes a self-contained site (GitHub Pages ready):
  index.html, app.js, style.css    the explorer (sigma.js overview, Cytoscape trees, sql.js)
  data/graph.json                  overview graph: packages + "API hub" nodes for mod names
                                   that only games provide, with dependency edges
  data/compat.json                 per-package status in the most popular games
  data/luanti-deps.sqlite          the full database, queried in the browser with sql.js
  data/meta.json                   build metadata

Graph and compatibility are computed here with the same resolver as `ldg resolve`, so the
explorer and the CLI always agree.
"""

import json
import sqlite3
from importlib import resources
from pathlib import Path

from . import db
from .resolve import Index, Resolver, Target, is_risky, package_refs

KIND_HARD, KIND_OPTIONAL, KIND_PROVIDED_BY = 0, 1, 2
STATUS_CODE = {"ok": "o", "risky": "r", "unsatisfiable": "u"}
COMPAT_GAMES = 12


def _graph(index: Index) -> dict:
    nodes: list[list] = []
    ids: dict[str, int] = {}

    def node(key: str, label: str, kind: str, score: float, downloads: int) -> int:
        if key not in ids:
            ids[key] = len(nodes)
            nodes.append([key, label, kind, round(score or 0, 1), downloads or 0])
        return ids[key]

    for p in sorted(index.by_id.values(), key=lambda p: -p.score):
        if p.type != "txp":
            node(p.key, p.title or p.name, p.type, p.score, p.downloads)

    edges: set[tuple[int, int, int]] = set()
    for p in index.by_id.values():
        if p.type == "txp":
            continue
        src = ids[p.key]
        for kind, names in ((KIND_HARD, p.depends - p.provides), (KIND_OPTIONAL, p.optional - p.provides)):
            for name in names:
                providers = index.providers(name)
                mods = [x for x in providers if x.type != "game"]
                if mods:
                    if mods[0].key != p.key:
                        edges.add((src, ids[mods[0].key], kind))
                elif providers:
                    hub = node(f"api:{name}", name, "api", 0, 0)
                    edges.add((src, hub, kind))
                    for game in providers:
                        edges.add((hub, ids[game.key], KIND_PROVIDED_BY))
    # A hard edge supersedes an optional one between the same pair.
    hard_pairs = {(s, t) for s, t, k in edges if k == KIND_HARD}
    edge_list = sorted(e for e in edges if not (e[2] == KIND_OPTIONAL and (e[0], e[1]) in hard_pairs))
    return {"fields": {"node": ["key", "label", "kind", "score", "downloads"], "edge": ["source", "target", "kind"]},
            "nodes": nodes, "edges": edge_list}


def _compat(index: Index) -> dict:
    games = index.default_games(COMPAT_GAMES)
    resolvers = [Resolver(index, g) for g in games]
    status: dict[str, str] = {}
    for p in index.by_id.values():
        if p.type != "mod":
            continue
        target = Target.from_pkg(p)
        refs = package_refs(index.conn, p)
        codes = []
        for game, resolver in zip(games, resolvers, strict=True):
            res = resolver.resolve(target)
            if not res.ok:
                codes.append("u")
                continue
            available = game.provides | target.provides | {
                m for entry in res.install for m in index.package(entry["package"]).provides}
            risky = any(is_risky(r, available) for r in refs)
            codes.append("r" if risky else "o")
        status[p.key] = "".join(codes)
    return {"games": [{"key": g.key, "name": g.name, "title": g.title} for g in games],
            "codes": {"o": "ok", "r": "risky", "u": "unsatisfiable"}, "status": status}


def export(db_file: Path, out: Path) -> dict:
    conn = db.connect(db_file)
    index = Index(conn)
    out.mkdir(parents=True, exist_ok=True)
    static = resources.files("luanti_depgraph") / "web"
    version = str(db.meta(conn).get("built_at", "dev")).replace(":", "").replace("+", "")
    for name in ("index.html", "app.js", "style.css"):
        text = (static / name).read_text(encoding="utf-8").replace("{{version}}", version)
        (out / name).write_text(text, encoding="utf-8")
    data = out / "data"
    data.mkdir(exist_ok=True)
    graph = _graph(index)
    compat = _compat(index)
    meta = db.meta(conn)
    (data / "graph.json").write_text(json.dumps(graph, separators=(",", ":")))
    (data / "compat.json").write_text(json.dumps(compat, separators=(",", ":")))
    (data / "meta.json").write_text(json.dumps(meta, indent=1))
    # A clean, compact copy of the database for the browser.
    target = data / "luanti-deps.sqlite"
    target.unlink(missing_ok=True)
    src = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    src.backup(dst)
    dst.execute("VACUUM")
    dst.close()
    src.close()
    (out / ".nojekyll").write_text("")
    return {"out": str(out), "nodes": len(graph["nodes"]), "edges": len(graph["edges"]),
            "compat_packages": len(compat["status"]), "games": [g["name"] for g in compat["games"]],
            "sqlite_bytes": target.stat().st_size}
