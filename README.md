# luanti-depgraph

A dependency graph of [Luanti](https://www.luanti.org) games, mods and modpacks. Use it from the command line (`ldg`), from agents through MCP, in the browser, or as a plain SQLite / Cypher dataset.

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
| Which package is it? | `ldg search "words from its title, description or tags"` |
| Download what it needs for game G, and which mods to enable | `ldg install author/name --game G --dest DIR` |
| How do working mods do X? | `ldg code search "node timer that grows a plant" [--game mineclonia]` |
| Anything else | `ldg sql "…"` or `ldg cypher "…"` |

A target is either a ContentDB package (`author/name`) or a local directory. Add `--json` for machine-readable output.

**Explorer:** https://culmat.github.io/luanti-depgraph/ shows the same data in the browser. It has the ecosystem graph, colors every mod by whether it works in a chosen game, shows dependency trees per package and the dependency history, and runs SQL in the browser.

## Install

```sh
uv tool install 'luanti-depgraph[search] @ git+https://github.com/culmat/luanti-depgraph'
ldg update        # download the nightly database snapshot (a few MB, no load on ContentDB)
ldg code update   # optional: the Lua code search index (~45 MB)
ldg info
```

Once 0.2.0 is on PyPI: `uv tool install 'luanti-depgraph[search]'` or `uvx luanti-depgraph …`.

The snapshot is rebuilt every night from ContentDB. Nothing is downloaded automatically: run `ldg update` to refresh, and `ldg info` to see how old your copy is. To build the database yourself, see [Development](docs/development.md#getting-data).

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

## How resolution works

`resolve` and `compat` follow the engine and the main-menu installer:

1. Every mod bundled with the game satisfies its name.
2. Any other hard dependency is installed from ContentDB. The package named exactly like the mod name is preferred, then other providers by score, with deprecated ones last.
3. Games never satisfy dependencies of other content.

Unlike the installer, `ldg` only picks a provider whose own dependencies can be met in that game. So the answer means "this is possible", not "this is what the menu would pick". For every missing name it explains why each candidate fails. The full rules are in [docs/resolution-rules.md](docs/resolution-rules.md).

## Documentation

| Document | For |
|---|---|
| [Usage](docs/usage.md) | Blockers, code search, installing dependencies |
| [Linting](docs/linting.md) | `ldg lint` findings (LDG001…) and how the analysis works |
| [Data model](docs/data-model.md) | Tables, views, the Cypher graph, the snapshot contract, where the data comes from |
| [Resolution rules](docs/resolution-rules.md) | Exactly what `resolve` and `compat` compute |
| [Development](docs/development.md) | Running the code from a checkout, building data, the explorer, tests, CI |
| [Contributing](CONTRIBUTING.md) | Issues, pull requests, and what a change has to keep working |

## License

MIT. ContentDB data belongs to its respective authors; see [content.luanti.org](https://content.luanti.org).

Part of [avanti-luanti](https://github.com/culmat/avanti-luanti).
