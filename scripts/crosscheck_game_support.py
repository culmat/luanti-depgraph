"""Compare ldg's per-game resolution with ContentDB's own detected game support.

ContentDB stores detected support (confidence 1) only for packages with game-specific hard
dependencies; such a package should be installable by `ldg resolve` in exactly those games.
Needs a database built with `ldg build --details`.

    uv run python scripts/crosscheck_game_support.py [--sample 50] [--seed 1]
"""

import argparse
import random
from collections import Counter

from luanti_depgraph import db, paths
from luanti_depgraph.resolve import Index, Resolver, Target


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", type=int, default=50)
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args()

    index = Index(db.connect(paths.db_path()))
    detected: dict[int, set[int]] = {}
    for pid, gid in index.conn.execute(
            "SELECT package_id, game_id FROM game_support WHERE confidence = 1 AND supports = 1"):
        detected.setdefault(pid, set()).add(gid)
    if not detected:
        raise SystemExit("No detected game support in the database; build with `ldg build --details`.")

    candidates = sorted(pid for pid in detected if index.by_id[pid].type == "mod")
    random.Random(args.seed).shuffle(candidates)
    sample = candidates[: args.sample]
    games = index.games
    resolvers = {g.id: Resolver(index, g) for g in games}

    def stale(pkg, gid, ldg_says) -> bool:
        """ContentDB's stored support disagrees with the game's *current* provides: the game
        added or dropped one of the package's direct hard dependencies after detection ran."""
        game_names = index.by_id[gid].provides
        direct = {n for n in pkg.depends - pkg.provides
                  if not any(p.type != "game" for p in index.providers(n))}
        return bool(direct) and (direct <= game_names) == ldg_says

    exact, explained, reasons, rows = 0, 0, Counter(), []
    for pid in sample:
        pkg = index.by_id[pid]
        target = Target.from_pkg(pkg)
        ours = {gid for gid, r in resolvers.items() if r.resolve(target).ok}
        ours -= index.declared_unsupported.get(pid, set())
        theirs = detected[pid]
        if ours == theirs:
            exact += 1
            continue
        unexplained = {g for g in ours - theirs if not stale(pkg, g, True)} | \
                      {g for g in theirs - ours if not stale(pkg, g, False)}
        if not unexplained:
            explained += 1
            reasons["ContentDB data stale for some games (game provides changed since detection)"] += 1
            continue
        only_ours = {index.by_id[g].name for g in (ours - theirs) & unexplained}
        only_theirs = {index.by_id[g].name for g in (theirs - ours) & unexplained}
        rows.append((pkg.key, sorted(only_ours)[:6], sorted(only_theirs)[:6]))
        reasons["unexplained difference"] += 1

    total = len(sample)
    print(f"{exact}/{total} identical ({100 * exact / total:.0f}%), {explained} differ only where "
          f"ContentDB's stored support is stale; agreement {100 * (exact + explained) / total:.0f}% "
          f"({len(games)} games considered)")
    for reason, n in reasons.most_common():
        print(f"  {reason}: {n}")
    for key, ours_only, theirs_only in rows:
        print(f"- {key}: only ldg {ours_only or '-'} | only ContentDB {theirs_only or '-'}")


if __name__ == "__main__":
    main()
