"""A LadybugDB (embedded Cypher graph database) view of the SQLite data.

SQLite stays the source of truth; the graph is rebuilt from it whenever it is
missing or older than the SQLite file, so LadybugDB's file format can change
between versions without anyone losing data.

Graph model:
  (:Package)-[:PROVIDES]->(:ModName)
  (:Package)-[:DEPENDS_ON {is_optional}]->(:ModName)
  (:Package)-[:CONTAINS]->(:Mod)-[:IMPLEMENTS]->(:ModName)
  (:Mod)-[:REQUIRES {is_optional}]->(:ModName)
  (:Mod)-[:REFERENCES {kind, symbol, count, declared, transitive, guarded}]->(:ModName)
  (:Package)-[:SUPPORTS {supports, confidence}]->(:Package {type: 'game'})
"""

import csv
import shutil
import sqlite3
import tempfile
from pathlib import Path

import ladybug as lb

SCHEMA = [
    "CREATE NODE TABLE Package(key STRING PRIMARY KEY, author STRING, name STRING, type STRING,"
    " title STRING, license STRING, downloads INT64, score DOUBLE, repo STRING)",
    "CREATE NODE TABLE ModName(name STRING PRIMARY KEY)",
    "CREATE NODE TABLE Mod(key STRING PRIMARY KEY, name STRING, path STRING, package STRING)",
    "CREATE REL TABLE PROVIDES(FROM Package TO ModName)",
    "CREATE REL TABLE DEPENDS_ON(FROM Package TO ModName, is_optional BOOLEAN)",
    "CREATE REL TABLE CONTAINS(FROM Package TO Mod)",
    "CREATE REL TABLE IMPLEMENTS(FROM Mod TO ModName)",
    "CREATE REL TABLE REQUIRES(FROM Mod TO ModName, is_optional BOOLEAN)",
    "CREATE REL TABLE REFERENCES(FROM Mod TO ModName, kind STRING, symbol STRING, count INT64,"
    " declared BOOLEAN, transitive BOOLEAN, guarded BOOLEAN)",
    "CREATE REL TABLE SUPPORTS(FROM Package TO Package, supports BOOLEAN, confidence INT64)",
]

PKG_KEY = "p.author || '/' || p.name"
MOD_KEY = f"{PKG_KEY} || '#' || m.name"

EXPORTS = {
    "Package": f"SELECT {PKG_KEY}, p.author, p.name, p.type, p.title, p.license,"
               " COALESCE(p.downloads, 0), COALESCE(p.score, 0), p.repo FROM package p",
    "ModName": "SELECT modname FROM provides UNION SELECT modname FROM dep"
               " UNION SELECT name FROM mod UNION SELECT modname FROM mod_dep UNION SELECT modname FROM ref",
    "Mod": f"SELECT {MOD_KEY}, m.name, m.path, {PKG_KEY} FROM mod m JOIN package p ON p.id = m.package_id",
    "PROVIDES": f"SELECT {PKG_KEY}, pr.modname FROM provides pr JOIN package p ON p.id = pr.package_id",
    "DEPENDS_ON": f"SELECT {PKG_KEY}, d.modname, d.optional FROM dep d JOIN package p ON p.id = d.package_id",
    "CONTAINS": f"SELECT {PKG_KEY}, {MOD_KEY} FROM mod m JOIN package p ON p.id = m.package_id",
    "IMPLEMENTS": f"SELECT {MOD_KEY}, m.name FROM mod m JOIN package p ON p.id = m.package_id",
    "REQUIRES": f"SELECT {MOD_KEY}, d.modname, d.optional FROM mod_dep d JOIN mod m ON m.id = d.mod_id"
                " JOIN package p ON p.id = m.package_id",
    "REFERENCES": f"SELECT {MOD_KEY}, r.modname, r.kind, r.symbol, r.count, r.declared, r.transitive, r.guarded"
                  " FROM ref r JOIN mod m ON m.id = r.mod_id JOIN package p ON p.id = m.package_id",
    "SUPPORTS": "SELECT p.author || '/' || p.name, g.author || '/' || g.name, s.supports, s.confidence"
                " FROM game_support s JOIN package p ON p.id = s.package_id JOIN package g ON g.id = s.game_id",
}
BOOLEAN_COLUMNS = {"DEPENDS_ON": {2}, "REQUIRES": {2}, "REFERENCES": {5, 6, 7}, "SUPPORTS": {2}}
# Mod keys must be unique; a package could in theory contain two mods with the same name.
DEDUPE = {"Mod", "CONTAINS", "IMPLEMENTS"}


NO_QUOTE = "\x01"  # never occurs in cleaned data; disables quote handling in the reader


def _clean(value):
    if isinstance(value, str):
        return " ".join(value.replace(NO_QUOTE, "").split())
    return "" if value is None else value


def build_graph(sqlite_file: Path, graph_file: Path) -> dict[str, int]:
    src = sqlite3.connect(f"file:{sqlite_file}?mode=ro", uri=True)
    tmp_graph = graph_file.with_name(graph_file.name + ".building")
    if tmp_graph.exists():
        tmp_graph.unlink() if tmp_graph.is_file() else shutil.rmtree(tmp_graph)
    counts: dict[str, int] = {}
    with tempfile.TemporaryDirectory() as tmp:
        db = lb.Database(str(tmp_graph))
        conn = lb.Connection(db)
        for statement in SCHEMA:
            conn.execute(statement)
        for table, query in EXPORTS.items():
            # Tab-separated without quoting: _clean() collapses all whitespace in text fields,
            # and LadybugDB's CSV reader mis-splits quoted fields that contain commas.
            path = Path(tmp) / f"{table}.csv"  # tab-delimited; the reader picks the format by extension
            seen = set()
            n = 0
            with path.open("w", newline="") as fh:
                writer = csv.writer(fh, delimiter="\t", quoting=csv.QUOTE_NONE, escapechar=None,
                                    quotechar=NO_QUOTE, lineterminator="\n")
                for row in src.execute(query):
                    row = list(row)
                    if table in DEDUPE:
                        if tuple(row[:2]) in seen:
                            continue
                        seen.add(tuple(row[:2]))
                    for i in BOOLEAN_COLUMNS.get(table, ()):
                        row[i] = "true" if row[i] else "false"
                    writer.writerow([_clean(v) for v in row])
                    n += 1
            counts[table] = n
            if n:
                conn.execute(f"COPY {table} FROM '{path}' (header=false, parallel=false, delim='\\t', "
                             f"quote='{NO_QUOTE}', escape='{NO_QUOTE}')")
        del conn
        del db
    src.close()
    if graph_file.exists():
        graph_file.unlink() if graph_file.is_file() else shutil.rmtree(graph_file)
    tmp_graph.rename(graph_file)
    for leftover in graph_file.parent.glob(tmp_graph.name + "*"):
        leftover.unlink() if leftover.is_file() else shutil.rmtree(leftover)
    return counts


def ensure_graph(sqlite_file: Path, graph_file: Path) -> None:
    if not graph_file.exists() or graph_file.stat().st_mtime < sqlite_file.stat().st_mtime:
        build_graph(sqlite_file, graph_file)


def cypher(graph_file: Path, query: str, limit: int = 200) -> dict:
    db = lb.Database(str(graph_file), read_only=True)
    conn = lb.Connection(db)
    result = conn.execute(query)
    columns = result.get_column_names()
    rows = []
    while result.has_next() and len(rows) < limit:
        rows.append(result.get_next())
    truncated = result.has_next()
    return {"columns": columns, "rows": rows, "truncated": truncated}
