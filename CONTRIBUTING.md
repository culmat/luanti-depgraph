# Contributing

Issues and pull requests are welcome.

- **False positives from `ldg lint`** are bugs. Report them with the mod and the line.
- **A wrong `resolve` or `compat` answer:** include the package, the game, and what the main-menu installer or the engine does instead.
- **Ideas that belong upstream in [ContentDB](https://github.com/luanti-org/contentdb)** are tracked in [avanti-luanti/docs/upstream.md](https://github.com/culmat/avanti-luanti/blob/main/docs/upstream.md).

## Working on the code

[docs/development.md](docs/development.md) explains setup, getting data, running the explorer and the MCP server from a checkout, and the CI workflows. In short:

```sh
uv sync --all-extras
export LDG_HOME=$PWD/.ldg
uv run ldg update            # or: uv run ldg build
uv run ldg site --serve      # look at it in the browser
```

Before opening a pull request:

```sh
uv run pytest
uv run ruff check src tests
```

## Changes that affect readers of the snapshot

The nightly snapshot is read by other programs, so two version numbers in its `meta` table are a contract (see [docs/data-model.md](docs/data-model.md#snapshot-contract)). `tests/test_contract.py` fails when either changes without a bump.

- **Schema:** adding a table or column only needs the new hash in `SCHEMA_HASH`. Renaming, removing or changing the meaning of one also needs `SCHEMA_VERSION` bumped in `db.py`.
- **Resolution rules:** any change to what `resolve` or `compat` answers needs `RULES_VERSION` bumped in `resolve.py`, a new entry in `RULES_HASH`, and the rule written out in [docs/resolution-rules.md](docs/resolution-rules.md).

  [luanthetti](https://github.com/culmat/luanthetti) ports the rules to Go and checks its port against every precomputed answer in each nightly snapshot. A rules change makes that check fail until the port catches up, so tell its maintainers before the change reaches `main`.
- **Data sources:** changes to what is fetched from ContentDB or package hosts should keep the load low (see [docs/data-model.md](docs/data-model.md#being-a-good-citizen)).

## AI assistance

This project is developed with AI assistance (Claude), reviewed by a human maintainer. Contributions made with AI tools are welcome on the same terms: you have read and understood what you submit.
