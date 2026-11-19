# Linting a mod

`ldg lint` checks a local mod, modpack or game: does it declare the dependencies its code uses, and does it meet ContentDB's basics? The MCP tool is `lint_mod`.

```console
$ ldg lint ./mymod
mymod (mod, 1 mod(s))
  error   LDG001 [mymod] init.lua:4 Uses 'default' without declaring it (default.node_sound_stone_defaults); …
          fix: Add default to depends, or to optional_depends and guard the code with core.get_modpath("default").
  warning LDG002 [mymod] init.lua:11 Checks for 'mcl_core' but doesn't list it in optional_depends, …
```

Use `--strict` in CI to exit 1 on errors, and `--author <you>` to skip name checks against your own packages.

## Findings

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

## How the analysis works

It parses Lua with tree-sitter and is deliberately conservative:

- A global counts as another mod's API only if that mod assigns a table to it outside any function (`mymod = {}`), or if it equals a known mod name. It must also be used as `X.y`, `X:y()` or `X[k]`.
- `if X then`, `X and …`, `get_modpath()` and `global_exists()` count as guards.
- Names reached through declared dependencies count as satisfied.
- Uses inside a callback stored under the mod's own key don't count, because only that mod calls them, e.g. `mcl_redstone.get_power` in `def._mcl_redstone = {update = function ... end}`.
- Item names that are only compared (`stack:get_name() == "X:item"`) don't count.
- A name declared local anywhere in a file is treated as local throughout it.

Expect false negatives rather than false positives. Please report false positives as issues.

The same analysis runs over the source corpus during `ldg build --git-top N`. Its results are the `mod`, `mod_global` and `ref` tables (see [data-model.md](data-model.md)), and they decide when `compat` calls a package `risky` (see [resolution-rules.md](resolution-rules.md#compatibility-status)).
