"""Guards for the snapshot contract: readers of the database (luanthetti, the explorer) pin
`meta.schema_version` and `meta.rules_version`. These tests fail when the schema or the
resolution rules change without a bump, so the bump is a deliberate decision."""

import hashlib
import json
from pathlib import Path

from luanti_depgraph import db, resolve
from luanti_depgraph.resolve import Resolver, Target, compat, package_refs

SRC = Path(resolve.__file__).parent

# Update these hashes together with the version they belong to.
SCHEMA_HASH = {1: "cac518a43115175d"}
RULES_HASH = {1: "d350c75de49dffe2"}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def test_schema_version_matches_schema():
    assert _sha(db.SCHEMA) == SCHEMA_HASH[db.SCHEMA_VERSION], (
        "db.SCHEMA changed. If a table or column was renamed, removed or changed meaning, bump "
        "SCHEMA_VERSION and add its hash; if the change is purely additive, update the hash for "
        f"the current version: {_sha(db.SCHEMA)!r}")


def test_rules_version_matches_resolver():
    text = (SRC / "resolve.py").read_text()
    assert _sha(text) == RULES_HASH[resolve.RULES_VERSION], (
        "resolve.py changed. If any answer of resolve/compat can change, bump RULES_VERSION, "
        "update docs/resolution-rules.md and add the hash; otherwise update the hash for the "
        f"current version: {_sha(text)!r}")


def test_meta_carries_versions(index):
    m = db.meta(index.conn)
    assert m["schema_version"] == db.SCHEMA_VERSION
    assert m["rules_version"] == resolve.RULES_VERSION
    assert m["compat_rows"] > 0 and m["precomputed_games"]


def test_precomputed_rows_match_live_resolver(index):
    """Every compat_matrix row equals what compat() says now, and every resolution row equals
    resolve(); the snapshot is the oracle a port compares itself with."""
    games = {g.id: g for g in index.games}
    for row in index.conn.execute("SELECT * FROM compat_matrix"):
        pkg, game = index.by_id[row["package_id"]], games[row["game_id"]]
        live = compat(index, Target.from_pkg(pkg), [game], package_refs(index.conn, pkg))[0]
        assert row["status"] == live["status"], (pkg.key, game.key)
        assert json.loads(row["missing"]) == live["missing"]
        assert json.loads(row["install"]) == live["install"]
        assert json.loads(row["undeclared_missing"]) == live["undeclared_missing"]
        assert json.loads(row["optional_available"]) == live["optional_available"]
    n = 0
    for row in index.conn.execute("SELECT * FROM resolution"):
        pkg, game = index.by_id[row["package_id"]], games[row["game_id"]]
        assert json.loads(row["json"]) == Resolver(index, game).resolve(Target.from_pkg(pkg)).as_dict()
        n += 1
    assert n > 0
