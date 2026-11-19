# Development

How to run `ldg` from a checkout, get data into it, look at the result, and what runs where.

## Setup

```sh
git clone https://github.com/culmat/luanti-depgraph
cd luanti-depgraph
uv sync --all-extras          # dev tools plus the [search] extra
export LDG_HOME=$PWD/.ldg     # a data directory for this checkout (gitignored)
```

`uv run ldg …` runs the code of your checkout. Use it for every command below; there are no other scripts or task runners.

Setting `LDG_HOME` keeps your development data apart from an installed `ldg` and its data in `~/.local/share/luanti-depgraph`. Without it, both use the same database.

## What runs where

Nothing runs as a server, except for a local web server while you look at the explorer.

```
ContentDB daily dump ─┐
package repositories ─┼─ ldg build ──▶ luanti-deps.sqlite ─┬─ ldg <command>     (reads the file directly)
previous database ────┘                                    ├─ ldg mcp           (stdio, started by the agent)
                                                           ├─ ldg graph         ──▶ luanti-deps.lbug (ldg cypher)
                                                           ├─ ldg site          ──▶ static explorer (any web server)
                                                           └─ ldg snapshot-pack ──▶ nightly release (ldg update)
corpus ─ ldg code index ──▶ luanti-code.sqlite (ldg code search)
```

- **CLI:** each command opens the SQLite file, answers, and exits.
- **MCP:** `ldg mcp` is a stdio server. The agent client starts it for a session and stops it afterwards.
- **Explorer:** static files plus data files, computed by `ldg site`. The page fetches its data, which browsers refuse over `file://`, so it needs a web server. `ldg site --serve` is one.
- **Nightly:** a GitHub Actions workflow builds and publishes the snapshot and the explorer. There is no backend.

## Getting data

There are two ways to fill `$LDG_HOME`:

| | `uv run ldg update` | `uv run ldg build` |
|---|---|---|
| Does | Downloads the published nightly snapshot | Builds the database from ContentDB with your code |
| Takes | Seconds | Seconds for metadata. With `--git-top 300`, several minutes the first time, then only changed packages |
| Use when you change | Code that **reads** the database: queries, resolver, lint, MCP tools, explorer | Code that **writes** it: `db.py`, `dump.py`, `corpus.py`, `analyze.py`, the schema, the precomputed tables |

A downloaded snapshot was built by the code on `main`. Its precomputed `compat_matrix` and `resolution` rows and its `meta.rules_version` follow `main`'s rules, while live answers (`ldg resolve`, the explorer's compatibility colors) come from your code. New columns your code adds only exist after a `build`.

### Building

```sh
uv run ldg update                  # optional, first: the build carries dependency history forward from it
uv run ldg build                   # ContentDB metadata from today's dump (~15 MB, seconds)
uv run ldg build --git-top 300     # plus sources and static analysis, as the nightly does
uv run ldg build --offline         # again from the cached dump or API responses, without contacting ContentDB
uv run ldg build --analyze-only    # rerun only the static analysis over the corpus you have
uv run ldg build --source api --details   # the API fallback path (~15 requests, plus ~2 per package)
uv run ldg code index              # the code search index from your corpus (downloads the model once)
```

**When new data is available:** ContentDB builds its dump at 05:00 UTC and it is out by about 05:30. `ldg build` takes today's dump once it is out, otherwise yesterday's, and keeps the newest one in `$LDG_HOME/cache/dump/`. Building again the same day costs one request to check whether a newer dump is out. Sources are fetched again only when a package's release changes.

**The data directory:**

| Path | Content |
|---|---|
| `luanti-deps.sqlite` | The database |
| `luanti-deps.lbug` | The Cypher graph, rebuilt from the database on first use |
| `luanti-code.sqlite` | The code search index |
| `cache/dump/` | The newest ContentDB dump |
| `cache/raw/` | The ContentDB lists the last build used (for `--offline`) |
| `cache/analysis/` | Static analysis results per package release |
| `corpus/<author>/<name>/` | Package sources, with a marker naming the release and commit |

## Looking at the result

```sh
uv run ldg info                                    # what the database contains, its versions and age
uv run ldg resolve mt-mods/pipeworks --game mineclonia
uv run ldg sql "SELECT key, value FROM meta"
uv run ldg site --serve                            # the explorer, with your data and code
```

`ldg site --serve` builds the explorer into `./site` (gitignored) from the database in `$LDG_HOME`. It then serves it at http://127.0.0.1:8000 and opens the browser. Ctrl-C stops it, and `--port` picks another port.

For the explorer with the latest data and your code:

```sh
uv run ldg update && uv run ldg build --git-top 300 && uv run ldg site --serve
```

A build analyses whatever is in the corpus, and `--git-top` fetches it. Leave it out once the corpus is filled to skip fetching (sources then stay at the releases you have); with an empty corpus there is no static analysis, so nothing shows as risky.

The explorer's code is in `src/luanti_depgraph/web/` (`index.html`, `app.js`, `style.css`) and its data in `src/luanti_depgraph/site.py`. After a change, run `ldg site --serve` again (about 4 s). The server tells the browser not to cache, so a reload shows the change.

### Trying the MCP server

Register the checkout as a second server next to an installed one:

```sh
claude mcp add ldg-dev -e LDG_HOME=$PWD/.ldg -- uv run --project $PWD ldg mcp
```

Or open it in the MCP Inspector:

```sh
npx @modelcontextprotocol/inspector -e LDG_HOME=$PWD/.ldg uv run ldg mcp
```

## Tests and lint

```sh
uv run pytest
uv run ruff check src tests
```

The tests build small databases from `tests/fixtures/` and use no network. `tests/test_contract.py` guards the snapshot contract; see [CONTRIBUTING.md](../CONTRIBUTING.md#changes-that-affect-readers-of-the-snapshot).

To compare the resolver with ContentDB's own detected game support, see [Checking the rules](resolution-rules.md#checking-the-rules).

## Automation

| Workflow | When | Does |
|---|---|---|
| `ci.yml` | Push to `main`, pull requests | `ruff` and `pytest` on Ubuntu and macOS, Python 3.11 and 3.13 |
| `nightly-snapshot.yml` | Daily, 05:47 UTC | `ldg update`, `ldg build --git-top 300 --details`, `ldg code index`, `ldg site`. It then publishes the `snapshot` release and the explorer |
| `pages.yml` | Push to `main` that changes the explorer | Redeploys the explorer with the latest published snapshot |
| `release.yml` | Tag `vX.Y.Z` matching the version in `pyproject.toml` | Tests, builds, publishes to PyPI and creates a GitHub release |

GitHub often starts scheduled runs hours late. To try the nightly on a branch without publishing:

```sh
gh workflow run nightly-snapshot.yml --ref <branch> -f publish=false
```

[luanthetti](https://github.com/culmat/luanthetti) runs a conformance check against each published snapshot.
