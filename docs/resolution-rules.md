# Resolution rules

What `ldg resolve` and `ldg compat` compute, in words. `resolve.py` implements these rules;
`RULES_VERSION` there is bumped whenever an answer changes, and the nightly snapshot records it
in `meta.rules_version` together with precomputed answers (`compat_matrix`, `resolution`).
Anything that re-implements the rules (luanthetti's Go port) checks itself against those rows.

The rules mirror the engine and the main-menu installer (`builtin/mainmenu/content/contentdb.lua`,
`src/content/mod_configuration.cpp`) and ContentDB's game-support detection
(`app/domain/game_support.py`).

## Inputs

- Every ContentDB package with the mod names it **provides** (every mod in a modpack or game) and
  the mod names it **depends on** (hard) or **optionally depends on**, both minus what it provides
  itself. Latest release only.
- **Declared game support** per package: `supported_games` / `unsupported_games` from the package
  `.conf` (confidence 10) or set on the website (confidence 11). ContentDB's detected support
  (confidence 1) is reported as `contentdb_says` but never used in the rules.
- `supports_all_games` per package.
- Static-analysis **references** for packages in the corpus: a mod's code uses another mod's
  API global, item string or `get_modpath` guard, with flags `declared`, `transitive`
  (loaded earlier through a declared dependency), `guarded`, and `requires` (mods the file
  needs to be loaded at all).

## Looking things up

- **Package** by `author/name` (also old aliases, case-insensitively), or by bare package name,
  preferring non-texture-packs and then the best score.
- **Game** by key, alias, name or title: trim, lowercase, strip a `_game` suffix, delete spaces.
  `minetest` therefore means Minetest Game.
- **Providers of a mod name**, in the installer's preference: the non-game package named like
  the mod, then other non-game providers by score, then games (which only count when they are
  the game being played). Ties keep database order.
- **Default games**: `minetest_game`, `mineclonia`, `mineclone2` when present, then the
  best-scored games, ten in all (fifteen for the precomputed tables).

## Resolving a target in a game

The target is a package (or a local directory). Its game is fixed.

1. Every mod the game bundles satisfies its name. For the games known to ship non-standard
   copies of Minetest Game mods (`pacman`, `tutorial`, `runorfall`, `realtest_mt5`, `mevo`,
   `xaenvironment`, `survivethedays`, `holidayhorrors`), the Minetest Game mod names are
   removed from that set, as ContentDB does, and a warning is added.
2. A hard dependency the game does not provide is **installed from ContentDB**: the first
   provider in installer order that is itself *satisfiable* in this game.
3. A provider is **satisfiable** when it is not a game, its declared game support allows this
   game (below), and each of its own hard dependencies is provided by the game or by a
   satisfiable provider. Dependency cycles are cut: a package already on the current path
   counts as not satisfiable, and a result that was influenced by a cut is not memoised.
4. **Declared support**: a provider that declares this game as unsupported never counts. A
   provider that declares *other* games, is not `supports_all_games`, and does not declare this
   game counts only if it is *not* game-independent (its hard dependencies cannot all be met by
   standalone mods). In other words, the declaration is decisive only when the dependency walk
   has nothing to say.
5. Installed providers bring their own hard dependencies, resolved the same way. A name is
   resolved once; the first resolution wins.
6. A name no satisfiable provider can supply is **missing**. The explanation lists up to ten
   non-game candidates with the dependencies that fail for them (or "dependency cycle"), the
   games that bundle the name, and how many providers exist.
7. The target's or an installed provider's mod names that the game also bundles are
   **shadowed**: the user mod replaces the game's mod, with a log warning in the engine.
8. The target's optional dependencies are **available** when the game, the target or an
   installed provider supplies them, otherwise **unavailable**.
9. The resolution is **ok** when nothing is missing. A game as target gets a warning: games are
   played, not installed into games.

## Compatibility status

For each game, after resolving:

- `unsatisfiable` when something is missing;
- otherwise `risky` when the target has an undeclared, unguarded, non-transitive reference of
  kind `global` to a mod name that is neither provided by the game (its full mod list, before
  removing non-standard MTG copies), the target, nor an installed provider, and the file making
  the reference would be loaded in this game (all its `requires` are available). Item-string
  references never make a package risky: an unknown item only leaves a recipe unused.
- otherwise `ok`.

`undeclared_missing` lists the risky references as `modname (kind)`. `contentdb_says` repeats
ContentDB's own claim for the pair, if any, with its confidence. A package outside the corpus
has no references and can only be `ok` or `unsatisfiable` (`analyzed_sources` is false).
