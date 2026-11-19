# Usage

The [README](../README.md) lists every question `ldg` answers. This page goes into the commands that need more than one line. Most commands take `--json`, and the MCP server exposes the same answers as tools.

## Blockers: what keeps mods out of a game

`ldg blockers --game G` resolves every ContentDB mod in game G, with the same rules as `resolve`, and ranks the mod names that make mods uninstallable there.

```console
$ ldg blockers --game mineclonia
ryvnf/mineclonia: 1274 of 3184 mods (40%) can't be installed, 6,001,063 downloads
  Root causes: default 1074 (sole blocker for 760), dye 95, player_api 89, wool 68, fire 62, …
```

- **`root_causes`** follow failing providers down to names that no standalone mod provides. For example, `technic` fails because it needs `default`, so the root cause is `default`.
- **`missing`** lists the names the installer can't satisfy directly.
- **`sole_blocker`** counts the mods that would become installable if that one name were available.

The MCP tool is `blockers`.

## Code search

```sh
ldg code update                                     # download the nightly index (~45 MB)
ldg code search "formspec with inventory list" --game mineclonia
```

`ldg code index` splits the corpus into the units modders think in: functions, `register_*` calls (nodes, crafts, entities, ABMs, commands, mobs, …), API tables, and runs of other code. Each chunk keeps its package, mod, file and lines.

Search combines SQLite FTS5 (Porter stemming, identifier parts, prefixes) with a small CPU-only code-embedding model ([potion-code-16M](https://huggingface.co/minishlab/potion-code-16M), via model2vec) using reciprocal rank fusion. At about 120k chunks a numpy dot product is faster than any vector database, and it runs everywhere, including on Intel Macs where zvec has no wheels.

`--game G` keeps only code from mods that work in G; `--kind` narrows to `function`, `register`, `table` or `code`. Needs the `[search]` extra.

## Installing what a mod needs

```sh
ldg install author/name --game mineclonia --dest ~/.minetest/mods [--with mtt]
```

Resolves the target in the game, downloads the release zips it needs (cached by release id) into `--dest`, and prints the `load_mod_*` lines to enable. Only the mods that are needed get enabled, also inside modpacks: enabling every mod of a modpack could pull in mods whose own dependencies aren't met, which the engine treats as fatal. `--with` adds mod names the target doesn't declare, such as a test framework.

## Finding packages

`ldg search` matches, in this order: the exact package or provided mod name, then `author/name` or title, then descriptions and tags (every query word must appear). Within each tier, higher-scored packages come first.

## Queries

`ldg sql` runs read-only SQL and `ldg cypher` read-only Cypher. The tables, views and graph are described in [data-model.md](data-model.md).
