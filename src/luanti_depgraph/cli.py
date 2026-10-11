"""`ldg`: query the Luanti dependency graph from the command line."""

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from . import __version__, db, graph, paths, queries, snapshot
from .lint import lint as run_lint
from .resolve import Index

app = typer.Typer(no_args_is_help=True, add_completion=False,
                  help="Dependency graph of Luanti games, mods and modpacks (data: ContentDB + sources).")
out = Console(width=None if sys.stdout.isatty() else 160)
err = Console(stderr=True)

JsonOpt = Annotated[bool, typer.Option("--json", help="Machine-readable output.")]
GamesOpt = Annotated[str | None, typer.Option("--games", help="Comma-separated games (default: top games).")]


def _emit_json(data) -> None:
    sys.stdout.write(json.dumps(data, indent=2, default=str) + "\n")


def _index() -> Index:
    try:
        return Index(db.connect(paths.db_path()))
    except FileNotFoundError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from None


def _split(value: str | None) -> list[str] | None:
    return [v.strip() for v in value.split(",") if v.strip()] if value else None


def _fail(exc: Exception) -> None:
    err.print(f"[red]{exc}[/red]")
    raise typer.Exit(1)


@app.command()
def version() -> None:
    """Print the version."""
    out.print(__version__)


@app.command()
def update(url: Annotated[str, typer.Option(help="Snapshot URL.")] = snapshot.SNAPSHOT_URL,
           as_json: JsonOpt = False) -> None:
    """Download the nightly snapshot (fast; doesn't touch ContentDB)."""
    result = snapshot.download(paths.db_path(), url)
    _emit_json(result) if as_json else out.print(f"Snapshot built {result['built_at']} → {result['path']}")


@app.command()
def build(
    details: Annotated[bool, typer.Option(help="Also fetch per-package details: license, engine range, "
                                               "ContentDB game support (~2 requests/package, cached).")] = False,
    offline: Annotated[bool, typer.Option(help="Reuse the last raw ContentDB responses.")] = False,
    git_top: Annotated[int, typer.Option(help="Fetch sources of the N best-scored mods/games.")] = 0,
    git_include: Annotated[str | None, typer.Option(
        help="Also fetch these (author/name or name), comma-separated.")] = None,
    git_all: Annotated[bool, typer.Option(help="Fetch sources of every mod and game.")] = False,
    analyze_only: Annotated[bool, typer.Option(help="Re-run static analysis on the existing corpus only.")] = False,
    workers: Annotated[int, typer.Option(help="Parallel source fetches.")] = 6,
    history_from: Annotated[Path | None, typer.Option(
        help="Previous database to carry dependency history from (default: the current one).")] = None,
    as_json: JsonOpt = False,
) -> None:
    """Build the database from ContentDB (and optionally package sources)."""
    log = (lambda msg: err.print(msg)) if as_json else (lambda msg: out.print(f"[dim]{msg}[/dim]"))
    stats = db.build(paths.db_path(), paths.cache_dir(), paths.corpus_dir(), details=details,
                     offline=offline, git_top=git_top, git_include=_split(git_include),
                     git_all=git_all, analyze_only=analyze_only, workers=workers,
                     history_from=history_from, log=log)
    _emit_json(stats) if as_json else out.print(stats)


@app.command()
def fetch(git_top: Annotated[int, typer.Option(help="Sources of the N best-scored mods/games.")] = 300,
          git_include: Annotated[str | None, typer.Option(
              help="Also these (author/name or name), comma-separated.")] = None,
          workers: int = 6, as_json: JsonOpt = False) -> None:
    """Fetch package sources into the corpus without rebuilding the database (2 requests + clones)."""
    from .contentdb import ContentDB
    from .corpus import fetch_many
    cdb = ContentDB(cache_dir=paths.cache_dir())
    raw = {"packages": cdb.packages(), "scores": cdb.scores()}
    selection = db.select_corpus_packages(raw, git_top, _split(git_include) or [], False)
    results = fetch_many(paths.corpus_dir(), selection, workers=workers)
    summary = {s: sum(r.status == s for r in results) for s in ("cloned", "zip", "unchanged", "failed")}
    summary["corpus"] = str(paths.corpus_dir())
    _emit_json(summary) if as_json else out.print(summary)


@app.command()
def info(as_json: JsonOpt = False) -> None:
    """Show what the local database contains and how old it is."""
    db_file = paths.db_path()
    try:
        meta = db.meta(db.connect(db_file))
    except FileNotFoundError as exc:
        _fail(exc)
    built = datetime.fromisoformat(meta["built_at"])
    meta["age_days"] = round((datetime.now(UTC) - built).total_seconds() / 86400, 1)
    meta["path"] = str(db_file)
    meta["corpus"] = str(paths.corpus_dir())
    if as_json:
        _emit_json(meta)
    else:
        for k, v in meta.items():
            out.print(f"[bold]{k:20}[/bold] {v}")


@app.command()
def providers(modname: str, as_json: JsonOpt = False) -> None:
    """Which packages provide a mod name (mods first, then games)?"""
    data = queries.providers(_index(), modname)
    if as_json:
        return _emit_json(data)
    out.print(f"[bold]{modname}[/bold]: {data['count']} provider(s)")
    for label, rows in (("standalone mods / modpacks", data["mods"]), ("games", data["games"])):
        if rows:
            out.print(f"  [underline]{label}[/underline] ({len(rows)})")
            for r in rows[:25]:
                out.print(f"    {r['package']:45} score {r['score']:>8}")
            if len(rows) > 25:
                out.print(f"    … {len(rows) - 25} more")
    if data["note"]:
        out.print(f"  [yellow]{data['note']}[/yellow]")


def _print_tree(nodes: list[dict], prefix: str = "") -> None:
    for i, node in enumerate(nodes):
        last = i == len(nodes) - 1
        branch = "└─ " if last else "├─ "
        via = node.get("via") or f"[red]only in games: {', '.join(node.get('only_in_games', [])) or 'none'}[/red]"
        extra = " [yellow](cycle)[/yellow]" if node.get("cycle") else ""
        out.print(f"{prefix}{branch}[bold]{node['modname']}[/bold] → {via}{extra}")
        _print_tree(node.get("depends", []), prefix + ("   " if last else "│  "))


def _print_resolution(res: dict) -> None:
    status = "[green]installable[/green]" if res["ok"] else "[red]NOT installable[/red]"
    out.print(f"[bold]{res['target']}[/bold] in [bold]{res['game']}[/bold]: {status}")
    if res["from_game"]:
        out.print(f"  from the game:  {', '.join(res['from_game'])}")
    for entry in res["install"]:
        alt = f" ({entry['alternatives']} other provider(s))" if entry["alternatives"] else ""
        out.print(f"  install:        {entry['package']} for {', '.join(entry['for'])}{alt}")
    for miss in res["missing"]:
        out.print(f"  [red]missing:[/red]        {miss['modname']} (needed by {miss['needed_by']}, "
                  f"{miss['providers']} provider(s) on ContentDB)")
        for cand in miss["candidates"][:5]:
            out.print(f"                    {cand['package']}: {cand['problem']}")
        if miss["game_providers"]:
            names = ", ".join(g.split("/")[1] for g in miss["only_in_games"][:6])
            more = f" +{miss['game_providers'] - 6}" if miss["game_providers"] > 6 else ""
            out.print(f"                    bundled in {miss['game_providers']} game(s) ({names}{more});"
                      " games can't be installed as dependencies")
    for sh in res["shadowed"]:
        out.print(f"  [yellow]overrides:[/yellow]      {sh['by']} {sh['note']}")
    if res["optional_available"]:
        out.print(f"  optional, available: {', '.join(res['optional_available'])}")
    if res["optional_unavailable"]:
        out.print(f"  optional, absent:    {', '.join(res['optional_unavailable'])}")
    for w in res["warnings"]:
        out.print(f"  [yellow]note:[/yellow] {w}")


@app.command()
def deps(target: Annotated[str, typer.Argument(help="author/name or a local path.")],
         tree: Annotated[bool, typer.Option(help="Show the transitive tree via best providers.")] = False,
         game: Annotated[str | None, typer.Option(help="Resolve inside this game.")] = None,
         as_json: JsonOpt = False) -> None:
    """Dependencies of a package or local mod, with who can provide each one."""
    try:
        data = queries.deps(_index(), target, tree=tree, game=game)
    except LookupError as exc:
        _fail(exc)
    if as_json:
        return _emit_json(data)
    out.print(f"[bold]{data['target']}[/bold] ({data['type']}) provides {', '.join(data['provides']) or '—'}")
    if data.get("alternatives_with_same_name"):
        out.print(f"  [dim]other packages named like this: {', '.join(data['alternatives_with_same_name'][:6])}[/dim]")
    table = Table("dependency", "kind", "mod providers", "game providers", "best mod provider", "in games")
    for d in data["depends"] + data["optional_depends"]:
        table.add_row(d["modname"], "optional" if d["optional"] else "[bold]hard[/bold]",
                      str(d["mod_providers"]), str(d["game_providers"]), d["best_mod"] or "[red]—[/red]",
                      ", ".join(d["top_games"][:4]))
    out.print(table)
    if "tree" in data:
        _print_tree(data["tree"])
    if "resolution" in data:
        _print_resolution(data["resolution"])


@app.command()
def rdeps(name: Annotated[str, typer.Argument(help="A mod name, or author/name for everything a package provides.")],
          transitive: bool = False,
          hard_only: Annotated[bool, typer.Option(help="Ignore optional dependers.")] = False,
          limit: int = 100, as_json: JsonOpt = False) -> None:
    """Who depends on a mod name (or on a package)?"""
    try:
        data = queries.rdeps(_index(), name, transitive=transitive, hard_only=hard_only, limit=limit)
    except LookupError as exc:
        _fail(exc)
    if as_json:
        return _emit_json(data)
    out.print(f"{data['count']} package(s) depend on {', '.join(data['modnames'][:8])}")
    table = Table("package", "type", "via", "kind", "depth")
    for r in data["dependents"]:
        table.add_row(r["package"], r["type"], r["via_modname"], "optional" if r["optional"] else "hard",
                      str(r["depth"]))
    out.print(table)


@app.command()
def resolve(target: Annotated[str, typer.Argument(help="author/name or a local path.")],
            game: Annotated[str, typer.Option(help="Game to resolve in, e.g. mineclonia.")],
            as_json: JsonOpt = False) -> None:
    """Can TARGET run in GAME, and what has to be installed? Explains failures."""
    try:
        data = queries.resolve(_index(), target, game)
    except LookupError as exc:
        _fail(exc)
    _emit_json(data) if as_json else _print_resolution(data)
    raise typer.Exit(0 if data["ok"] else 1)


@app.command()
def blockers(game: Annotated[str, typer.Option(help="Game to check, e.g. mineclonia.")],
             limit: Annotated[int, typer.Option(help="Rows per table.")] = 20,
             as_json: JsonOpt = False) -> None:
    """Which mod names keep mods from being installed in GAME, ranked by mods blocked?"""
    try:
        data = queries.blockers(_index(), game, limit=limit)
    except LookupError as exc:
        _fail(exc)
    if as_json:
        return _emit_json(data)
    share = data["unsatisfiable"] / data["mods"] if data["mods"] else 0
    out.print(f"[bold]{data['game']}[/bold]: {data['unsatisfiable']} of {data['mods']} mods ({share:.0%}) "
              f"can't be installed, {data['unsatisfiable_downloads']:,} downloads")
    for key, title in (("root_causes", "Root causes (names no standalone mod provides here)"),
                       ("missing", "Missing (what the installer can't satisfy)")):
        table = Table("modname", "blocked", "sole blocker", "downloads", "mod providers", "game providers",
                      "examples", title=title)
        for r in data[key]:
            table.add_row(r["modname"], str(r["blocked"]), str(r["sole_blocker"]), f"{r['downloads']:,}",
                          str(r["mod_providers"]), str(r["game_providers"]), ", ".join(r["examples"][:3]))
        out.print(table)


def _print_compat(rows: list[dict]) -> None:
    colors = {"ok": "green", "risky": "yellow", "unsatisfiable": "red"}
    table = Table("game", "status", "missing", "undeclared refs missing", "installs", "ContentDB says")
    for r in rows:
        claim = r["contentdb_says"]
        claim_text = "—" if claim is None else f"{'yes' if claim['supports'] else 'no'} (c={claim['confidence']})"
        table.add_row(r["game"], f"[{colors[r['status']]}]{r['status']}[/]", ", ".join(r["missing"]),
                      ", ".join(r["undeclared_missing"][:4]), ", ".join(r["install"][:3]), claim_text)
    out.print(table)


@app.command()
def compat(target: Annotated[str, typer.Argument(help="author/name or a local path.")],
           games: GamesOpt = None, as_json: JsonOpt = False) -> None:
    """Compatibility matrix of TARGET across games (dependencies + undeclared references)."""
    try:
        data = queries.compat_report(_index(), target, _split(games))
    except LookupError as exc:
        _fail(exc)
    if as_json:
        return _emit_json(data)
    out.print(f"[bold]{data['target']}[/bold]" + ("" if data["analyzed_sources"] else
              " [dim](sources not in corpus: dependency check only)[/dim]"))
    _print_compat(data["games"])


@app.command()
def lint(path: Annotated[Path, typer.Argument(help="Mod, modpack or game directory.")] = Path("."),
         author: Annotated[str | None, typer.Option(help="Your ContentDB username (skips your own names).")] = None,
         games: GamesOpt = None,
         strict: Annotated[bool, typer.Option(help="Exit 1 on errors (for CI).")] = False,
         quick: Annotated[bool, typer.Option(help="Skip the per-game matrix.")] = False,
         as_json: JsonOpt = False) -> None:
    """Check declared vs. actually used dependencies of a local mod."""
    try:
        conn = db.connect(paths.db_path())
        index = None if quick else Index(conn)
    except FileNotFoundError:
        conn, index = None, None
        err.print("[yellow]No database: running without ContentDB knowledge (run `ldg update`).[/yellow]")
    try:
        report = run_lint(path.expanduser(), conn, index, author=author, games=_split(games))
    except LookupError as exc:
        _fail(exc)
    if as_json:
        _emit_json(report)
    else:
        pkg = report["package"]
        out.print(f"[bold]{pkg['name']}[/bold] ({pkg['type']}, {len(pkg['mods'])} mod(s))")
        colors = {"error": "red", "warning": "yellow", "info": "blue"}
        for f in report["findings"]:
            where = f" {f['file']}:{f['line']}" if f["file"] else ""
            out.print(f"  [{colors[f['severity']]}]{f['severity']:7}[/] {f['code']} {escape('[' + f['mod'] + ']')}"
                      f"{escape(where)} {escape(f['message'])}")
            if f["fix"]:
                out.print(f"          [dim]fix: {escape(f['fix'])}[/dim]")
        if report["games"]:
            _print_compat(report["games"])
        c = report["counts"]
        out.print(f"{c['error']} error(s), {c['warning']} warning(s), {c['info']} info")
    if strict and report["counts"]["error"]:
        raise typer.Exit(1)


@app.command("name-check")
def name_check(modname: str,
               author: Annotated[str | None, typer.Option(help="Your ContentDB username.")] = None,
               as_json: JsonOpt = False) -> None:
    """Is a technical mod name free on ContentDB (policy 3.1 'right to a name')?"""
    data = queries.name_check(_index(), modname, author)
    if as_json:
        _emit_json(data)
    elif not data["valid"]:
        out.print(f"[red]'{modname}' is not a valid mod name (only a-z, 0-9 and _).[/red]")
    elif data["available"]:
        extra = f" (bundled in games: {', '.join(data['bundled_in_games'][:5])})" if data["bundled_in_games"] else ""
        out.print(f"[green]'{modname}' is free on ContentDB[/green]{extra}")
    else:
        out.print(f"[red]'{modname}' is taken by {', '.join(data['used_by'][:5])}[/red]")
    raise typer.Exit(0 if data["available"] else 1)


@app.command("globals")
def globals_cmd(path: Annotated[Path, typer.Argument()] = Path("."), as_json: JsonOpt = False) -> None:
    """Globals of a mod's declared dependencies, for .luacheckrc read_globals."""
    data = queries.luacheck_globals(db.connect(paths.db_path()), path.expanduser())
    if as_json:
        return _emit_json(data)
    out.print("read_globals = {")
    for g in data["read_globals"]:
        out.print(f'    "{g}",')
    out.print("}")


@app.command()
def search(query: str, type_: Annotated[str | None, typer.Option("--type", help="mod, game or txp")] = None,
           limit: int = 20, as_json: JsonOpt = False) -> None:
    """Find packages by name, title or provided mod name."""
    rows = queries.search(_index(), query, type_, limit)
    if as_json:
        return _emit_json(rows)
    table = Table("package", "type", "title", "score")
    for r in rows:
        table.add_row(r["package"], r["type"], r["title"], str(r["score"]))
    out.print(table)


def _print_rows(data: dict) -> None:
    table = Table(*[str(c) for c in data["columns"]])
    for row in data["rows"]:
        table.add_row(*[str(v) for v in row])
    out.print(table)
    if data["truncated"]:
        out.print("[dim](truncated)[/dim]")


@app.command()
def sql(query: str, limit: int = 200, as_json: JsonOpt = False) -> None:
    """Run a read-only SQL query (tables: package, provides, dep, mod, mod_dep, ref, … views v_*)."""
    try:
        data = queries.sql(db.connect(paths.db_path()), query, limit)
    except (ValueError, Exception) as exc:
        _fail(exc)
    _emit_json(data) if as_json else _print_rows(data)


@app.command()
def cypher(query: str, limit: int = 200, as_json: JsonOpt = False) -> None:
    """Run a read-only Cypher query on the LadybugDB graph (built on first use)."""
    graph.ensure_graph(paths.db_path(), paths.graph_path())
    try:
        data = graph.cypher(paths.graph_path(), query, limit)
    except RuntimeError as exc:
        _fail(exc)
    _emit_json(data) if as_json else _print_rows(data)


@app.command("graph")
def graph_cmd(as_json: JsonOpt = False) -> None:
    """(Re)build the LadybugDB graph from the SQLite database."""
    counts = graph.build_graph(paths.db_path(), paths.graph_path())
    _emit_json(counts) if as_json else out.print(counts)


@app.command()
def export(fmt: Annotated[str, typer.Option("--format", help="json or graphml")] = "json",
           output: Annotated[Path, typer.Option("--out")] = Path("luanti-deps.json")) -> None:
    """Export packages, provides and dependencies for other tools."""
    conn = db.connect(paths.db_path())
    packages = [dict(r) for r in conn.execute(
        "SELECT author || '/' || name AS key, author, name, type, title, score, downloads, license FROM package")]
    provides = [list(r) for r in conn.execute(
        "SELECT p.author || '/' || p.name, pr.modname FROM provides pr JOIN package p ON p.id = pr.package_id")]
    dependencies = [list(r) for r in conn.execute(
        "SELECT p.author || '/' || p.name, d.modname, d.optional FROM dep d JOIN package p ON p.id = d.package_id")]
    if fmt == "json":
        output.write_text(json.dumps({"meta": db.meta(conn), "packages": packages, "provides": provides,
                                      "depends": dependencies}))
    elif fmt == "graphml":
        from xml.sax.saxutils import quoteattr
        lines = ['<?xml version="1.0" encoding="UTF-8"?>',
                 '<graphml xmlns="http://graphml.graphdrawing.org/xmlns">',
                 '<key id="kind" for="node" attr.name="kind" attr.type="string"/>',
                 '<key id="type" for="node" attr.name="type" attr.type="string"/>',
                 '<key id="rel" for="edge" attr.name="rel" attr.type="string"/>',
                 '<graph edgedefault="directed">']
        modnames = {m for _, m in provides} | {m for _, m, _ in dependencies}
        for p in packages:
            lines.append(f'<node id={quoteattr("pkg:" + p["key"])}><data key="kind">package</data>'
                         f'<data key="type">{p["type"]}</data></node>')
        for m in sorted(modnames):
            lines.append(f'<node id={quoteattr("mod:" + m)}><data key="kind">modname</data></node>')
        for key, m in provides:
            lines.append(f'<edge source={quoteattr("pkg:" + key)} target={quoteattr("mod:" + m)}>'
                         '<data key="rel">provides</data></edge>')
        for key, m, optional in dependencies:
            rel = "optional_depends" if optional else "depends"
            lines.append(f'<edge source={quoteattr("pkg:" + key)} target={quoteattr("mod:" + m)}>'
                         f'<data key="rel">{rel}</data></edge>')
        lines += ["</graph>", "</graphml>"]
        output.write_text("\n".join(lines))
    else:
        _fail(ValueError("--format must be json or graphml"))
    out.print(f"wrote {output}")


@app.command()
def install(target: Annotated[str, typer.Argument(help="author/name or a local path.")],
            game: Annotated[str, typer.Option(help="Game the content will run in.")],
            dest: Annotated[Path, typer.Option(help="Directory to put the mods in (e.g. a LUANTI_MOD_PATH).")],
            with_: Annotated[str | None, typer.Option("--with", help="Extra mod names to require, e.g. mtt.")] = None,
            as_json: JsonOpt = False) -> None:
    """Download what TARGET needs in GAME into DEST and list the mods to enable (load_mod_*)."""
    from .install import install as run_install
    try:
        data = run_install(_index(), target, game, dest.expanduser(), paths.cache_dir(), _split(with_))
    except LookupError as exc:
        _fail(exc)
    if as_json:
        _emit_json(data)
    elif not data["ok"]:
        _print_resolution(data["resolution"])
    else:
        for item in data["installed"]:
            out.print(f"installed {item['package']} → {item['path']}")
        out.print(f"enable: {' '.join(data['enable'])}")
    raise typer.Exit(0 if data["ok"] else 1)


@app.command()
def site(output: Annotated[Path, typer.Option("--out", help="Directory for the static site.")] = Path("site"),
         as_json: JsonOpt = False) -> None:
    """Build the static dependency explorer (GitHub Pages ready) from the database."""
    from .site import export
    stats = export(paths.db_path(), output.expanduser())
    _emit_json(stats) if as_json else out.print(stats)


code_app = typer.Typer(no_args_is_help=True, help="Lua-aware code search over the package corpus.")
app.add_typer(code_app, name="code")


@code_app.command("index")
def code_index(as_json: JsonOpt = False) -> None:
    """Chunk and embed the corpus (functions, register_* calls, API tables)."""
    from .codesearch import build_index, code_db_path
    log = (lambda msg: err.print(msg)) if as_json else (lambda msg: out.print(f"[dim]{msg}[/dim]"))
    try:
        stats = build_index(paths.corpus_dir(), code_db_path(paths.db_path()), log=log)
    except RuntimeError as exc:
        _fail(exc)
    _emit_json(stats) if as_json else out.print(stats)


@code_app.command("update")
def code_update(url: Annotated[str, typer.Option(help="Code index URL.")] = snapshot.CODE_URL) -> None:
    """Download the nightly code index."""
    from .codesearch import code_db_path
    out.print(snapshot.download_plain(code_db_path(paths.db_path()), url))


@code_app.command("search")
def code_search(query: str,
                kind: Annotated[str | None, typer.Option(help="function, register, table or code")] = None,
                package: Annotated[str | None, typer.Option(help="Only this author/name.")] = None,
                game: Annotated[str | None, typer.Option(help="Only mods that work in this game.")] = None,
                limit: int = 10, as_json: JsonOpt = False) -> None:
    """Find real-world Lua code: "register a falling node", "formspec with inventory list"…"""
    from .codesearch import CodeSearch, code_db_path
    packages = None
    if game:
        from .resolve import working_packages
        index = _index()
        g = index.game(game)
        packages = working_packages(index, g) | {g.key}
    try:
        results = CodeSearch(code_db_path(paths.db_path())).search(query, limit, kind, package, packages=packages)
    except (RuntimeError, FileNotFoundError) as exc:
        _fail(exc)
    if as_json:
        return _emit_json(results)
    for r in results:
        out.print(f"[bold]{r['package']}[/bold] {r['file']}:{r['lines']}  [dim]{r['kind']} {escape(r['symbol'])}[/dim]")
        out.print(escape(r["snippet"]), highlight=False)
        out.print()


@app.command()
def mcp() -> None:
    """Run the MCP server on stdio (for Claude Code and other agents)."""
    from .mcp_server import main
    main()


@app.command("snapshot-pack", hidden=True)
def snapshot_pack(output: Annotated[Path, typer.Option("--output")] = Path("luanti-deps.sqlite.zst")) -> None:
    """Compress the database for publishing (used by the nightly workflow)."""
    snapshot.compress(paths.db_path(), output)
    out.print(f"wrote {output}")


def main() -> None:
    app()
