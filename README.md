# luanti-depgraph

A dependency graph of [Luanti](https://www.luanti.org) (formerly Minetest) games, mods and modpacks. Use it from the command line (`ldg`), from agents through MCP, or as a plain SQLite / Cypher dataset.

```console
$ ldg resolve TenPlus1/ethereal --game mineclonia
TenPlus1/ethereal in ryvnf/mineclonia: NOT installable
  missing:        default (needed by TenPlus1/ethereal, 37 provider(s) on ContentDB)
                    bundled in 37 game(s) (minetest_game, asuna, capturetheflag, …); games can't be installed as dependencies
```

## Why

Reasoning about Luanti dependencies is hard. Four reasons:

- **A dependency names a mod name, not a package.**
  - `depends = default` can be satisfied by 37 different games on ContentDB, and by no standalone mod.
  - Games bundle mods, reuse each other's mod names, and a user mod with the same name silently replaces a game's mod.
- **ContentDB tracks dependencies per package.**
  - A modpack counts as one unit.
  - Game support is worked out by matching names over hard dependencies only.
- **Mods often use other mods without declaring them.** For example, they call `default.*` or use `"default:stone"` in recipes. They work in one game and crash or silently break in others. The engine's `random_mod_load_order` setting exists precisely to shake these out.
- **Declared game support is advisory.** The engine ignores `supported_games` and `min_minetest_version`.

`ldg` combines ContentDB's metadata with static analysis of the package sources to answer these questions:

| Question | Command |
|---|---|
| Who provides mod name `X`? | `ldg providers X` |
| What does a package need, and who can provide each piece? | `ldg deps author/name [--tree]` |
| Can it run in game G, what has to be installed, and if not, why not? | `ldg resolve author/name --game G` |
| In which games does it work? | `ldg compat author/name` |
| Who depends on `X`, directly or transitively? | `ldg rdeps X [--transitive]` |
| Which mod names keep the most mods out of game G? | `ldg blockers --game G` |
| Does my mod declare what it uses? | `ldg lint path/to/mod` |
| Is a mod name still free on ContentDB? | `ldg name-check mymod` |
| Download what it needs for game G, and which mods to enable | `ldg install author/name --game G --dest DIR` |
| How do working mods do X? | `ldg code search "node timer that grows a plant" [--game mineclonia]` |
| Anything else | `ldg sql "…"` or `ldg cypher "…"` |

A target is either a ContentDB package (`author/name`) or a local directory.

**Explorer:** https://culmat.github.io/luanti-depgraph/ shows the same data in the browser. It has the ecosystem graph, colors every mod by whether it works in a chosen game, shows dependency trees per package and the dependency history, and runs SQL in the browser. `ldg site --out DIR` builds it (static, rebuilt nightly).

## Install

```sh
uv tool install 'luanti-depgraph[search] @ git+https://github.com/culmat/luanti-depgraph'
ldg update        # download the nightly database snapshot (a few MB, no load on ContentDB)
ldg code update   # optional: the Lua code search index (~45 MB)
ldg info
```

Once 0.2.0 is on PyPI: `uv tool install 'luanti-depgraph[search]'` or `uvx luanti-depgraph …`.

To build the database yourself instead:

```sh
ldg build                       # ContentDB metadata only: ~15 requests, about a minute
ldg build --git-top 300         # also fetch sources of the 300 best-scored mods/games for static analysis
ldg build --details             # also license, engine range and ContentDB's own game-support claims
```

**Data location:** `~/.local/share/luanti-depgraph`. Override it with `LDG_HOME`, or set `LDG_DB` and `LDG_CORPUS` individually.

## Use with agents (MCP)

`ldg mcp` runs a stdio MCP server. Every tool is read-only.

```sh
claude mcp add luanti-depgraph -- ldg mcp
```

**Tools:**
- `search_code` (Lua-aware code search; needs the `[search]` extra and `ldg code update`)
- `providers`, `deps`, `rdeps`, `resolve`, `compat`, `blockers`
- `lint_mod`, `name_check`, `search_packages`
- `query_sql`, `query_cypher`, `database_info`

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

Add `--json` for machine-readable output. The MCP tool is `blockers`.

## Linting a mod

```console
$ ldg lint ./mymod
mymod (mod, 1 mod(s))
  error   LDG001 [mymod] init.lua:4 Uses 'default' without declaring it (default.node_sound_stone_defaults); …
          fix: Add default to depends, or to optional_depends and guard the code with core.get_modpath("default").
  warning LDG002 [mymod] init.lua:11 Checks for 'mcl_core' but doesn't list it in optional_depends, …
```

| Code | Severity | Meaning |
|---|---|---|
| LDG001 | error | Uses another mod's API (`X.foo`, `X:bar()`) without declaring the mod |
| LDG002 | warning | Checks for a mod (`get_modpath`, `if X then`) without listing it in `optional_depends`, so load order isn't guaranteed |
| LDG003 | warning | Uses another mod's item names (`"X:item"`) without declaring it |
| LDG005 | info | Declares a hard dependency that the code never seems to use |
| LDG006 | error | Hard dependency that no ContentDB package provides |
| LDG007 | warning | Legacy `depends.txt` |
| LDG008 | info | Uses the `minetest` namespace (`core` is preferred) |
| LDG009 | warning | Mod name already taken on ContentDB (policy 3.1, "right to a name") |
| LDG010 | warning/info | Missing `mod.conf`, description or LICENSE |
| LDG011 | warning | Lua parse error |
| LDG012 | info | Uses mods it only gets through other dependencies |

Use `--strict` in CI to exit 1 on errors, and `--author <you>` to skip name checks against your own packages.

**How the analysis works.** It parses Lua with tree-sitter and is deliberately conservative:
- A global counts as another mod's API only if that mod assigns a table to it outside any function (`mymod = {}`), or if it equals a known mod name. It must also be used as `X.y`, `X:y()` or `X[k]`.
- `if X then`, `X and …`, `get_modpath()` and `global_exists()` count as guards.
- Names reached through declared dependencies count as satisfied.
- A name declared local anywhere in a file is treated as local throughout it.

Expect false negatives rather than false positives. Please report false positives as issues.

## Code search

`ldg code index` splits the corpus into the units modders think in: functions, `register_*` calls (nodes, crafts, entities, ABMs, commands, mobs, …), API tables, and runs of other code. Each chunk keeps its package, mod, file and lines. Search combines SQLite FTS5 (Porter stemming, identifier parts, prefixes) with a small CPU-only code-embedding model ([potion-code-16M](https://huggingface.co/minishlab/potion-code-16M), via model2vec) using reciprocal rank fusion. At about 120k chunks a numpy dot product is faster than any vector database, and it runs everywhere, including on Intel Macs where zvec has no wheels.

## Data model

Data is stored in SQLite, the source of truth (`ldg sql`):

| Table | Content |
|---|---|
| `package` | Every approved ContentDB package, with score, downloads, repo, plus license and engine range after `--details` |
| `package_alias` | Old `author/name` keys pointing to their current package |
| `provides`, `dep` | Package-level provides and dependencies, as ContentDB reports them |
| `mod`, `mod_dep` | Individual mods found in the sources, with their own dependencies (modpacks split up) |
| `mod_global` | API globals a mod defines |
| `ref` | References from a mod's code to other mod names (`global`, `itemstring`, `modpath_guard`), each flagged as declared, transitive or guarded |
| `game_support` | ContentDB's own game-support claims (`--details`) |
| `dep_history`, `provides_history` | Intervals in which a package had a dependency or provided a mod name, carried forward across builds (the nightly downloads the previous snapshot first) |

Views: `v_providers`, `v_package_deps`, `v_game_modnames`, `v_undeclared_refs`, `v_transitive_deps`.

The same data is also available as a graph in [LadybugDB](https://github.com/LadybugDB/ladybug), an embedded Cypher database (`ldg cypher`, rebuilt from SQLite on demand):

```cypher
(:Package)-[:PROVIDES]->(:ModName)
(:Package)-[:DEPENDS_ON {is_optional}]->(:ModName)
(:Package)-[:CONTAINS]->(:Mod)-[:IMPLEMENTS|REQUIRES|REFERENCES]->(:ModName)
(:Package)-[:SUPPORTS {supports, confidence}]->(:Package)
```

```sh
ldg cypher "MATCH (p:Package)-[:DEPENDS_ON {is_optional: false}]->(:ModName {name: 'default'}) RETURN count(p)"
```

## How resolution works

`resolve` and `compat` follow the engine and the main-menu installer:

1. Every mod bundled with the game satisfies its name.
2. Any other hard dependency is installed from ContentDB. The package named exactly like the mod name is preferred, then other providers by score.
3. Games never satisfy dependencies of other content.

Unlike the installer, `ldg` only picks a provider whose own dependencies can be met in that game. So the answer means "this is possible", not "this is what the menu would pick". For every missing name it explains why each candidate fails.

### Accuracy

`scripts/crosscheck_game_support.py` needs a database built with `--details`. It compares `resolve` with ContentDB's own detected game support for a sample of mods, and lists every difference so it can be explained.

## Being a good citizen

- **Load on ContentDB:** the client identifies itself, never exceeds 2 requests per second, and caches package details by release ID. Normal use relies on the nightly snapshot (`ldg update`), which is built once a day in GitHub Actions and published as a release asset.
- **Sources:** shallow, blob-filtered sparse clones, so only Lua and metadata files are fetched. ContentDB release zips are used only for packages without a repo, at most two at a time.

## Contributing

Issues and PRs are welcome.

```sh
uv sync
uv run pytest
uv run ruff check src tests
```

Ideas that belong upstream in [ContentDB](https://github.com/luanti-org/contentdb) are tracked in [avanti-luanti/docs/upstream.md](https://github.com/culmat/avanti-luanti/blob/main/docs/upstream.md).

This project is developed with AI assistance (Claude), reviewed by a human maintainer. Part of [avanti-luanti](https://github.com/culmat/avanti-luanti).

## License

MIT. ContentDB data belongs to its respective authors; see [content.luanti.org](https://content.luanti.org).
