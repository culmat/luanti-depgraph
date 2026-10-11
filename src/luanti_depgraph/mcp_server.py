"""MCP server exposing the dependency graph to agents. Every tool is read-only."""

from functools import cache
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from . import db, graph, paths, queries
from .lint import lint as run_lint
from .resolve import Index

INSTRUCTIONS = """\
Luanti (formerly Minetest) dependency graph built from ContentDB and package sources.
Key idea: a dependency names a *mod name* (an API), not a package. Many packages can
provide the same name; games bundle mods and satisfy names for content played in them.
Typical flow: name_check before creating a mod; deps/providers to understand a
dependency; resolve/compat to see in which games something can run; lint_mod on a
local directory to find undeclared dependencies; blockers for which names keep mods
out of a whole game. Targets are "author/name" or an absolute local path.
Games accept names like minetest_game, mineclonia, mineclone2.
"""

server = MCPServer("luanti-depgraph", instructions=INSTRUCTIONS)
READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)


@cache
def _index() -> Index:
    return Index(db.connect(paths.db_path()))


@server.tool(annotations=READ_ONLY)
def providers(modname: str) -> dict:
    """List the packages (standalone mods first, then games) that provide a technical mod name."""
    return queries.providers(_index(), modname)


@server.tool(annotations=READ_ONLY)
def deps(target: str, tree: bool = False, game: str | None = None) -> dict:
    """Dependencies of a package ("author/name") or local mod path, with provider counts.
    tree=True expands transitive hard dependencies; game=... resolves inside that game."""
    return queries.deps(_index(), target, tree=tree, game=game)


@server.tool(annotations=READ_ONLY)
def rdeps(name: str, transitive: bool = False, hard_only: bool = False, limit: int = 100) -> dict:
    """Packages that depend on a mod name, or on anything a package ("author/name") provides."""
    return queries.rdeps(_index(), name, transitive=transitive, hard_only=hard_only, limit=limit)


@server.tool(annotations=READ_ONLY)
def resolve(target: str, game: str) -> dict:
    """Can the target run in this game? Returns what the game provides, what to install, and
    for each missing dependency why every candidate provider fails."""
    return queries.resolve(_index(), target, game)


@server.tool(annotations=READ_ONLY)
def compat(target: str, games: list[str] | None = None) -> dict:
    """Per-game compatibility (ok / risky / unsatisfiable) including undeclared references
    found by static analysis. Defaults to the most popular games."""
    return queries.compat_report(_index(), target, games)


@server.tool(annotations=READ_ONLY)
def blockers(game: str, limit: int = 30) -> dict:
    """Ecosystem view for one game: which mod names keep ContentDB mods from being installed
    there, ranked by mods blocked. root_causes follow failing providers down to names no
    standalone mod provides (e.g. `default` in Mineclonia); missing is what the installer lacks."""
    return queries.blockers(_index(), game, limit=limit)


@server.tool(annotations=READ_ONLY)
def lint_mod(path: str, author: str | None = None, games: list[str] | None = None) -> dict:
    """Analyze a local mod/modpack/game directory: undeclared or unused dependencies, legacy
    files, name conflicts on ContentDB, and a per-game compatibility matrix."""
    index = _index()
    return run_lint(Path(path).expanduser(), index.conn, index, author=author, games=games)


@server.tool(annotations=READ_ONLY)
def name_check(modname: str, author: str | None = None) -> dict:
    """Check whether a technical mod name is valid and still free on ContentDB."""
    return queries.name_check(_index(), modname, author)


@server.tool(annotations=READ_ONLY)
def search_packages(query: str, type: str | None = None, limit: int = 20) -> list[dict]:
    """Search packages by name, title or provided mod name. type: mod, game or txp."""
    return queries.search(_index(), query, type, limit)


@server.tool(annotations=READ_ONLY)
def query_sql(query: str, limit: int = 200) -> dict:
    """Read-only SQL. Tables: package, package_alias, provides, dep, mod, mod_dep, mod_global,
    ref, game_support, meta. Views: v_package, v_providers, v_package_deps, v_game_modnames,
    v_undeclared_refs, v_transitive_deps (filter on root)."""
    return queries.sql(_index().conn, query, limit)


@server.tool(annotations=READ_ONLY)
def query_cypher(query: str, limit: int = 200) -> dict:
    """Read-only Cypher on the graph: (:Package)-[:PROVIDES|DEPENDS_ON {is_optional}]->(:ModName),
    (:Package)-[:CONTAINS]->(:Mod)-[:IMPLEMENTS|REQUIRES|REFERENCES]->(:ModName),
    (:Package)-[:SUPPORTS {supports, confidence}]->(:Package)."""
    graph.ensure_graph(paths.db_path(), paths.graph_path())
    return graph.cypher(paths.graph_path(), query, limit)


@cache
def _code():
    from .codesearch import CodeSearch, code_db_path
    return CodeSearch(code_db_path(paths.db_path()))


@server.tool(annotations=READ_ONLY)
def search_code(query: str, kind: str | None = None, package: str | None = None, limit: int = 10) -> list[dict]:
    """Find real Lua code in the top ContentDB packages and games, chunked by function,
    register_* call (nodes, crafts, entities, ABMs, commands, mobs…) and API table.
    kind: function | register | table | code. Use it to learn how others solve something,
    e.g. "register a node with a node timer" or "mcl_sounds stone sounds"."""
    return _code().search(query, limit, kind, package)


@server.tool(annotations=READ_ONLY)
def database_info() -> dict:
    """When the database was built and what it covers."""
    return db.meta(_index().conn) | {"path": str(paths.db_path())}


def main() -> None:
    server.run("stdio")
