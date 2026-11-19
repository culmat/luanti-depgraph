# Data model

Everything `ldg` knows is in one SQLite database (`ldg sql`). A graph view of it in LadybugDB (`ldg cypher`) is derived from it on demand. The nightly snapshot that `ldg update` downloads is this database.

## Where the data comes from

- **ContentDB metadata** comes from ContentDB's [daily database dump](https://content.luanti.org/help/backups/), built at 05:00 UTC. It has every approved package with its dependencies, details and full release list, so a build makes one ~15 MB download from ContentDB's CDN instead of thousands of API requests.
  - The dump format is marked experimental upstream. If it can't be downloaded or doesn't look as expected, the build falls back to the API and says so (`ldg build --source auto`, the default). `--source dump` fails instead.
  - `meta.contentdb_source` records which one was used, and `meta.dump_created_at` the dump's date.
- **Package sources** (`ldg build --git-top N`) are fetched for the N best-scored mods and games and analysed statically (see [linting.md](linting.md)).
  - Fetches are shallow, blob-filtered and sparse, so only Lua and metadata files are transferred.
  - They check out the commit of the latest ContentDB release, so the analysis sees the code players install. The default branch is used only when a release has no commit or the host won't serve it.
  - Packages without a repo are downloaded as release zips from ContentDB's CDN, checked against their sha256, at most two at a time.
- **History** (`dep_history`, `provides_history`) is carried forward from the previous database at the same path; the nightly downloads the previous snapshot first.

### Being a good citizen

- Builds read the daily dump, one CDN download, instead of the API.
- When the API is used, the client identifies itself, never exceeds 2 requests per second, and caches package details by release ID.
- Normal use relies on the nightly snapshot (`ldg update`), built once a day in GitHub Actions and published as a release asset, so most users never touch ContentDB.

### Freshness

The nightly workflow (05:47 UTC, after the dump is out) rebuilds the database and publishes it twice: as the `snapshot` release asset that `ldg update` downloads, and on the [explorer](https://culmat.github.io/luanti-depgraph/). The explorer's About page shows both the data version and the version of the explorer itself.

## Tables

| Table | Content |
|---|---|
| `meta` | What the database is: `built_at`, `ldg_version`, `ldg_commit`, `schema_version`, `rules_version`, source, counts |
| `package` | Every approved ContentDB package, with score, downloads, repo, license, engine range, `dev_state`, description, tags, dates, and issue tracker, forum and website links. `corpus_status` says how its sources were obtained: `pinned` (release commit), `cloned` (default branch), `zip`, `unchanged`, `cached`, `failed`, or NULL when not fetched |
| `package_maintainer` | ContentDB users who maintain each package |
| `package_alias` | Old `author/name` keys pointing to their current package |
| `package_release` | Every approved release: date, git commit, engine range, downloads, zip size and sha256 |
| `provides`, `dep` | Package-level provides and dependencies, as ContentDB reports them |
| `mod`, `mod_dep` | Individual mods found in the sources, with their own dependencies (modpacks split up) |
| `mod_global` | API globals a mod defines |
| `ref` | References from a mod's code to other mod names (`global`, `itemstring`, `modpath_guard`), each flagged as declared, transitive or guarded |
| `game_support` | ContentDB's own game-support claims |
| `dep_history`, `provides_history` | Intervals in which a package had a dependency or provided a mod name |
| `compat_matrix`, `resolution` | The resolver's answers, precomputed (see [Snapshot contract](#snapshot-contract)) |

Views:

| View | Content |
|---|---|
| `v_package` | `package` with an `author/name` column |
| `v_providers` | Who provides each mod name, with `exact_name` for the package named like it |
| `v_package_deps` | Each package's dependencies by `author/name` |
| `v_transitive_deps` | Hard dependencies followed through every non-game provider, with depth |
| `v_game_modnames` | The mod names each game bundles |
| `v_undeclared_refs` | References a mod's code makes to mod names it doesn't declare |
| `v_dep_changes` | Dependencies added and removed over time (`current` or `removed`) |
| `v_release_engine_changes` | Releases that changed the minimum engine version |

```sh
ldg sql "SELECT package, released_at, previous_min_engine, min_engine FROM v_release_engine_changes ORDER BY released_at DESC LIMIT 10"
```

## Graph

The same data as a graph in [LadybugDB](https://github.com/LadybugDB/ladybug), an embedded Cypher database (`ldg cypher`, rebuilt from SQLite on demand, or with `ldg graph`):

```cypher
(:Package)-[:PROVIDES]->(:ModName)
(:Package)-[:DEPENDS_ON {is_optional}]->(:ModName)
(:Package)-[:CONTAINS]->(:Mod)-[:IMPLEMENTS|REQUIRES|REFERENCES]->(:ModName)
(:Package)-[:SUPPORTS {supports, confidence}]->(:Package)
(:User)-[:MAINTAINS]->(:Package)
```

```sh
ldg cypher "MATCH (p:Package)-[:DEPENDS_ON {is_optional: false}]->(:ModName {name: 'default'}) RETURN count(p)"
```

## Snapshot contract

The database describes itself in `meta`, including two version numbers that readers pin:

- **`schema_version`** changes when a table or column is renamed, removed or changes meaning. Additions don't bump it.
- **`rules_version`** changes whenever `resolve.py` would give a different answer. The rules are written out in [resolution-rules.md](resolution-rules.md).

`tests/test_contract.py` fails when either changes without a bump.

Two tables carry the resolver's answers precomputed for every non-game package in the fifteen default games. A reader of the snapshot then doesn't need the rules at all, and a port of the rules ([luanthetti](https://github.com/culmat/luanthetti), Go) can check itself against every row:

- `compat_matrix(package_id, game_id, status, missing, install, undeclared_missing, optional_available)`
- `resolution(package_id, game_id, json)`: the full `resolve` answer, stored where it says more than the matrix row.
