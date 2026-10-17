"""Lua-aware code search over the package corpus.

Generic code-search tools split Lua into plain text windows. Here every chunk is a unit a
modder thinks in: a function, a `register_*` call (node, craft, entity, ABM, chat command,
mob, ...), an API table, or a run of other top-level statements. Each chunk records its
package, mod, file, lines, kind and symbol (function name or registered item name).

Ranking fuses SQLite FTS5 (BM25) with semantic similarity from a small static code-embedding
model (model2vec, CPU only, no API key), using reciprocal rank fusion. At this scale (tens of
thousands of chunks) a brute-force numpy dot product beats running a vector database.
The index is a separate SQLite file next to the main database (`luanti-code.sqlite`).

Needs the optional extra: `pip install 'luanti-depgraph[search]'`.
"""

import os
import re
import sqlite3
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import tree_sitter_lua
from tree_sitter import Language, Node, Parser

from .luanticonf import scan_package

MODEL = "minishlab/potion-code-16M"
MAX_TEXT = 3000
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
MAX_EMBED = 2000
CODE_WINDOW_LINES = 40
_PARSER = Parser(Language(tree_sitter_lua.language()))
_REGISTER_RE = re.compile(r"(^|[.:])register_\w+$")
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{1,}")
_STOPWORDS = set("""a an and are as at be by can do does for from get how i in is it its of on or set that the
this to use used using when where which while with what make made want my me into you your all any""".split())
SKIP_DIRS = {"spec", "test", "tests", "mtt", ".git"}

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE chunk (
    id INTEGER PRIMARY KEY,
    package TEXT NOT NULL,      -- author/name
    mod TEXT NOT NULL,
    file TEXT NOT NULL,         -- relative to the package
    line_start INTEGER NOT NULL,
    line_end INTEGER NOT NULL,
    kind TEXT NOT NULL,         -- function | register | table | code
    symbol TEXT NOT NULL,       -- function name, registered name ("mymod:thing") or table name
    text TEXT NOT NULL
);
CREATE INDEX chunk_package ON chunk (package);
CREATE VIRTUAL TABLE chunk_fts USING fts5(symbol, text, content='chunk', content_rowid='id',
                                          tokenize="porter unicode61 tokenchars ':'");
CREATE TABLE vec (id INTEGER PRIMARY KEY, emb BLOB NOT NULL);  -- int8, unit vector * 127
"""


def _require_extra():
    try:
        import numpy  # noqa: F401
        from model2vec import StaticModel  # noqa: F401
    except ImportError as exc:
        raise RuntimeError("Code search needs the optional extra: "
                           "uv tool install 'luanti-depgraph[search]'  (or pip install model2vec numpy)") from exc


def code_db_path(main_db: Path) -> Path:
    return main_db.with_name("luanti-code.sqlite")


# ------------------------------------------------------------------ chunking

def _first_string(call: Node) -> str | None:
    args = call.child_by_field_name("arguments")
    if args is None:
        return None
    for child in args.named_children:
        if child.type == "string":
            content = next((c for c in child.named_children if c.type == "string_content"), None)
            return content.text.decode(errors="replace") if content is not None else None
        return None
    return None


def _register_call(node: Node) -> Node | None:
    """The outermost register_* call in a top-level statement, if any."""
    stack = [node]
    while stack:
        n = stack.pop(0)
        if n.type == "function_call":
            callee = n.child_by_field_name("name")
            if callee is not None and _REGISTER_RE.search(callee.text.decode(errors="replace")):
                return n
        if n.type in ("function_declaration", "function_definition"):
            continue  # registrations inside helper functions belong to the function chunk
        stack.extend(n.children)
    return None


def _assigned_table(node: Node) -> str | None:
    """`name = {…}` or `local name = {…}` (also `x = x or {…}`): the assigned name."""
    assignment = node.named_children[0] if node.type == "variable_declaration" and node.named_children else node
    if assignment.type != "assignment_statement":
        return None
    targets = next((c for c in assignment.named_children if c.type == "variable_list"), None)
    values = next((c for c in assignment.named_children if c.type == "expression_list"), None)
    if targets is None or values is None or not values.named_children:
        return None
    value = values.named_children[0]
    if value.type == "table_constructor" or (
            value.type == "binary_expression" and any(c.type == "table_constructor" for c in value.named_children)):
        return targets.text.decode(errors="replace")[:80]
    return None


def chunk_source(source: bytes) -> list[dict]:
    tree = _PARSER.parse(source)
    lines = source.decode(errors="replace").splitlines()
    chunks: list[dict] = []
    pending: list[Node] = []

    def text_of(start: int, end: int) -> str:
        return "\n".join(lines[start:end + 1])[:MAX_TEXT]

    def flush() -> None:
        while pending:
            group = [pending.pop(0)]
            while pending and pending[0].end_point[0] - group[0].start_point[0] < CODE_WINDOW_LINES:
                group.append(pending.pop(0))
            start, end = group[0].start_point[0], group[-1].end_point[0]
            if "".join(lines[start:end + 1]).strip():
                chunks.append({"kind": "code", "symbol": "", "line_start": start + 1, "line_end": end + 1,
                               "text": text_of(start, end)})

    for node in tree.root_node.named_children:
        if node.type == "comment":
            continue
        start, end = node.start_point[0], node.end_point[0]
        kind = symbol = None
        if node.type == "function_declaration":
            name = node.child_by_field_name("name")
            kind, symbol = "function", name.text.decode(errors="replace") if name is not None else ""
        else:
            call = _register_call(node)
            if call is not None:
                callee = call.child_by_field_name("name").text.decode(errors="replace")
                kind, symbol = "register", _first_string(call) or callee
                symbol = f"{callee.rsplit('.', 1)[-1].rsplit(':', 1)[-1]} {symbol}".strip()
            elif (target := _assigned_table(node)) is not None and end > start:
                kind, symbol = "table", target  # one-line tables stay with the surrounding code
        if kind is None:
            pending.append(node)
            continue
        flush()
        chunks.append({"kind": kind, "symbol": symbol, "line_start": start + 1, "line_end": end + 1,
                       "text": text_of(start, end)})
    flush()
    return chunks


def iter_corpus_chunks(corpus: Path):
    for marker in sorted(corpus.glob("*/*/.ldg-release")):
        pkg_dir = marker.parent
        key = f"{pkg_dir.parent.name}/{pkg_dir.name}"
        pkg = scan_package(pkg_dir)
        for mod in pkg.mods:
            for path in sorted(mod.dir.rglob("*.lua")):
                parts = path.relative_to(mod.dir).parts
                if any(p in SKIP_DIRS or p.startswith(".") for p in parts):
                    continue
                if len(parts) > 1 and (mod.dir / parts[0] / "init.lua").is_file():
                    continue  # a nested mod, handled on its own
                data = path.read_bytes()
                if len(data) > 1_000_000:
                    continue
                rel = path.relative_to(pkg.root).as_posix()
                for chunk in chunk_source(data):
                    yield {"package": key, "mod": mod.name, "file": rel, **chunk}


# ------------------------------------------------------------------ index

def build_index(corpus: Path, out: Path, log: Callable = print, batch: int = 2048) -> dict:
    _require_extra()
    import numpy as np
    from model2vec import StaticModel

    started = time.monotonic()
    model = StaticModel.from_pretrained(MODEL)
    seconds_model = time.monotonic() - started
    tmp = out.with_name(out.name + ".building")
    tmp.unlink(missing_ok=True)
    conn = sqlite3.connect(tmp)
    conn.executescript(SCHEMA)
    rows, texts, count = [], [], 0
    seconds_embed = 0.0

    def write() -> None:
        nonlocal rows, texts, seconds_embed
        if not rows:
            return
        cur = conn.executemany(
            "INSERT INTO chunk (package, mod, file, line_start, line_end, kind, symbol, text)"
            " VALUES (:package, :mod, :file, :line_start, :line_end, :kind, :symbol, :text)", rows)
        del cur
        first_id = conn.execute("SELECT max(id) FROM chunk").fetchone()[0] - len(rows) + 1
        embed_started = time.monotonic()
        vectors = model.encode(texts)
        seconds_embed += time.monotonic() - embed_started
        vectors = vectors / np.maximum(np.linalg.norm(vectors, axis=1, keepdims=True), 1e-9)
        # int8 quantization of unit vectors: half the size of float16, ranking barely changes.
        quantized = np.clip(np.round(vectors * 127), -127, 127).astype(np.int8)
        conn.executemany("INSERT INTO vec VALUES (?, ?)",
                         [(first_id + i, v.tobytes()) for i, v in enumerate(quantized)])
        rows, texts = [], []

    loop_started = time.monotonic()
    for chunk in iter_corpus_chunks(corpus):
        rows.append(chunk)
        texts.append(f"{chunk['kind']} {chunk['symbol']}\n{chunk['file']}\n{chunk['text']}"[:MAX_EMBED])
        count += 1
        if len(rows) >= batch:
            write()
            if count % (batch * 5) == 0:
                log(f"  {count} chunks")
    write()
    seconds_loop = time.monotonic() - loop_started
    fts_started = time.monotonic()
    conn.execute("INSERT INTO chunk_fts (chunk_fts) VALUES ('rebuild')")
    seconds_fts = time.monotonic() - fts_started
    stats = {"built_at": datetime.now(UTC).isoformat(timespec="seconds"), "model": MODEL, "chunks": count,
             "packages": conn.execute("SELECT count(DISTINCT package) FROM chunk").fetchone()[0],
             "kinds": dict(conn.execute("SELECT kind, count(*) FROM chunk GROUP BY kind").fetchall()),
             "seconds_model": round(seconds_model, 1),
             # Parsing and chunking the sources, and inserting rows: everything in the loop but embedding.
             "seconds_chunk": round(seconds_loop - seconds_embed, 1),
             "seconds_embed": round(seconds_embed, 1),
             "seconds_fts": round(seconds_fts, 1),
             "build_seconds": round(time.monotonic() - started, 1)}
    conn.executemany("INSERT INTO meta VALUES (?, ?)", [(k, str(v)) for k, v in stats.items()])
    conn.commit()
    conn.execute("VACUUM")
    conn.close()
    tmp.replace(out)
    return stats


# ------------------------------------------------------------------ query

class CodeSearch:
    def __init__(self, path: Path):
        _require_extra()
        import numpy as np
        from model2vec import StaticModel

        if not path.exists():
            raise FileNotFoundError(f"No code index at {path}. Run `ldg code update` or `ldg code index`.")
        self.np = np
        self.conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        ids, blobs = zip(*self.conn.execute("SELECT id, emb FROM vec ORDER BY id"), strict=True) or ((), ())
        self.ids = np.array(ids)
        self.matrix = np.frombuffer(b"".join(blobs), dtype=np.int8).reshape(len(ids), -1).astype(np.float32) / 127
        self.model = StaticModel.from_pretrained(MODEL)

    def _fts(self, query: str, limit: int) -> list[int]:
        tokens = [t for t in _TOKEN_RE.findall(query) if t.lower() not in _STOPWORDS]
        if not tokens:
            return []
        # Prefix terms: "particles" -> particl* also matches add_particlespawner's "particlespawner".
        parts = [p for t in tokens[:24] for p in t.split("_") if len(p) > 2 and p.lower() not in _STOPWORDS]
        match = " OR ".join(f'"{p}"*' for p in parts) or " OR ".join(f'"{t}"' for t in tokens[:24])
        return [r[0] for r in self.conn.execute(
            "SELECT rowid FROM chunk_fts WHERE chunk_fts MATCH ? ORDER BY bm25(chunk_fts, 2.0, 1.0) LIMIT ?",
            (match, limit))]

    def _semantic(self, query: str, limit: int) -> list[int]:
        np = self.np
        q = self.model.encode([query])[0]
        q = q / max(float(np.linalg.norm(q)), 1e-9)
        scores = self.matrix @ q
        top = np.argpartition(-scores, min(limit, len(scores) - 1))[:limit]
        top = top[np.argsort(-scores[top])]
        return [int(self.ids[i]) for i in top]

    def search(self, query: str, limit: int = 10, kind: str | None = None, package: str | None = None,
               mod: str | None = None, packages: set[str] | None = None) -> list[dict]:
        pool = 400
        fused: dict[int, float] = {}
        for ranking in (self._fts(query, pool), self._semantic(query, pool)):
            for rank, cid in enumerate(ranking):
                fused[cid] = fused.get(cid, 0.0) + 1.0 / (60 + rank)
        results = []
        for cid in sorted(fused, key=fused.get, reverse=True):
            row = self.conn.execute("SELECT * FROM chunk WHERE id = ?", (cid,)).fetchone()
            if (kind and row["kind"] != kind) or (package and row["package"] != package) or \
                    (mod and row["mod"] != mod) or (packages is not None and row["package"] not in packages):
                continue
            lines = row["text"].splitlines()
            results.append({"package": row["package"], "mod": row["mod"], "file": row["file"],
                            "lines": f"{row['line_start']}-{row['line_end']}", "kind": row["kind"],
                            "symbol": row["symbol"], "score": round(fused[cid], 4),
                            "snippet": "\n".join(lines[:25]) + ("\n…" if len(lines) > 25 else "")})
            if len(results) >= limit:
                break
        return results

    def meta(self) -> dict:
        return {r["key"]: r["value"] for r in self.conn.execute("SELECT key, value FROM meta")}
